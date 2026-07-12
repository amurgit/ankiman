from __future__ import annotations

import os
import re
import resource
import signal
import sys
import time
from dataclasses import dataclass
from typing import Any

import typer
from loguru import logger

from .anki import (
    AnkiConnectClient,
    AnkiConnectError,
    extract_field_values,
    preview_field_text,
    resolve_deck_name,
)
from .config import AppConfig, ModelConfig, config_path, ensure_api_key, load_config, save_config
from .llm import LLMClient, check_balance, parse_ai_response

PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")


def extract_source_fields(prompt: str) -> list[str]:
    seen: set[str] = set()
    fields: list[str] = []
    for match in PLACEHOLDER_RE.finditer(prompt):
        name = match.group(1)
        if name not in seen:
            seen.add(name)
            fields.append(name)
    return fields


def replace_placeholders(text: str, field_map: dict[str, str]) -> str:
    def repl(match: re.Match[str]) -> str:
        return field_map.get(match.group(1), "")

    return PLACEHOLDER_RE.sub(repl, text)


def parse_comma_list(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def count_nonempty(values: dict[str, str], names: list[str]) -> int:
    return sum(1 for name in names if values.get(name, "").strip())


def should_skip_source(
    fields: dict[str, str],
    source_fields: list[str],
    *,
    allow_partial: bool,
) -> str | None:
    if not source_fields:
        return "prompt has no {Field} placeholders"
    total = len(source_fields)
    filled = count_nonempty(fields, source_fields)
    if filled == 0:
        return "all source fields empty"
    if filled < total and not allow_partial:
        empty = [name for name in source_fields if not fields.get(name, "").strip()]
        return f"source field(s) empty: {', '.join(empty)}"
    return None


def should_skip_target(
    fields: dict[str, str],
    target_fields: list[str],
    *,
    force: bool,
) -> str | None:
    if force:
        return None
    if all(fields.get(target, "").strip() for target in target_fields):
        return "all target fields already filled"
    return None


def validate_target_fields(
    notes: list[dict[str, Any]],
    target_fields: list[str],
    source_fields: list[str],
) -> None:
    if not notes:
        return
    by_model: dict[str, set[str]] = {}
    for note in notes:
        model = str(note.get("modelName", "unknown"))
        by_model.setdefault(model, set()).update(extract_field_values(note).keys())
    for model, available in by_model.items():
        for field in target_fields + source_fields:
            if field not in available:
                raise SystemExit(
                    f"Field {field!r} does not exist on note type {model!r}.\n"
                    f"Create fields in the Anki app (Tools → Manage Note Types → Fields) "
                    f"before running fill."
                )


@dataclass
class FillStats:
    total: int = 0
    processed: int = 0
    skipped: int = 0
    errors: int = 0



def _do_ping(check_key: bool, config: str | None) -> None:
    client = AnkiConnectClient()
    ver = client.version()
    decks = client.deck_names()
    logger.info("AnkiConnect OK (version {}) — {} deck(s)", ver, len(decks))
    if check_key:
        app_cfg = load_config()
        model_cfg = app_cfg.resolve(config)
        key = ensure_api_key(model_cfg.api_key_env, prompt=False)
        logger.info("API key {} is set ({}…)", model_cfg.api_key_env, key[:4])


def _do_deck_list() -> None:
    client = AnkiConnectClient()
    names = sorted(client.deck_names())
    logger.info("{:>3}  {:<40}  Notes", "#", "Deck")
    for i, name in enumerate(names, start=1):
        count = len(client.find_notes(f'deck:"{name}"'))
        logger.info("{:>3}  {:<40}  {}", i, name, count)


def _do_deck_fields(deck: str) -> None:
    client = AnkiConnectClient()
    deck_name = resolve_deck_name(client, deck)
    note_ids = client.find_notes(f'deck:"{deck_name}"')
    if not note_ids:
        logger.info("No notes in deck {!r}", deck_name)
        return

    notes = client.notes_info(note_ids)
    by_model: dict[str, dict[str, Any]] = {}
    for note in notes:
        model = str(note.get("modelName", "unknown"))
        fields = extract_field_values(note)
        entry = by_model.setdefault(model, {"fields": set(), "example": fields})
        entry["fields"].update(fields.keys())

    logger.info(
        "Deck {!r}: {} note(s), {} note type(s)",
        deck_name,
        len(notes),
        len(by_model),
    )
    logger.info("Use these field names in prompt placeholders like {{Field}} and in --target-fields.")

    for model in sorted(by_model):
        info = by_model[model]
        example_fields: dict[str, str] = info["example"]
        logger.info("Note type: {}", model)
        for field_name in sorted(info["fields"]):
            logger.info(
                "  {:<24} {}",
                field_name,
                preview_field_text(example_fields.get(field_name, "")),
            )


def _do_model_add(
    name: str,
    model: str,
    api_base: str,
    api_key_env: str = "",
    set_default: bool = False,
    force: bool = False,
) -> None:
    cfg_path = config_path()
    if cfg_path.is_file():
        app_cfg = load_config(cfg_path)
    else:
        app_cfg = AppConfig(default_model=name, models={})
    if name in app_cfg.models and not force:
        raise SystemExit(f"Model {name!r} already exists. Use --force to overwrite.")
    app_cfg.models[name] = ModelConfig(
        name=name,
        model=model,
        api_base=api_base.rstrip("/"),
        api_key_env=api_key_env,
    )
    if set_default:
        app_cfg.default_model = name
    save_config(app_cfg, cfg_path)
    logger.info(
        "Saved model {name!r} (model={model!r}, api_base={api_base!r}) to {path}",
        name=name,
        model=model,
        api_base=api_base.rstrip("/"),
        path=cfg_path,
    )
    if set_default:
        logger.info("Default model set to {name!r}", name=name)


def _process_result(
    note_id: int,
    fields: dict[str, str],
    raw: str,
    tfields: list[str],
    sfields: list[str],
    dry_run: bool,
    stats: FillStats,
    eligible: int,
    client: AnkiConnectClient,
) -> None:
    logger.log("DETAIL", "Note {} raw response:\n{}", note_id, raw)
    try:
        updates = parse_ai_response(raw, tfields)
    except ValueError as exc:
        stats.errors += 1
        logger.error("Note {}: {}", note_id, exc)
        return
    stats.processed += 1

    tag = "[DRY RUN] " if dry_run else ""
    counter = f"[{stats.processed}/{eligible}]"
    source_str = ", ".join(f"{name}={preview_field_text(fields.get(name, ''))}" for name in sfields)
    target_str = ", ".join(f"{name}={preview_field_text(updates.get(name, ''))}" for name in tfields)
    logger.info("{} {} {} -> {}", counter, tag, source_str, target_str)

    if not dry_run:
        try:
            client.update_note_fields(note_id, updates)
        except AnkiConnectError as exc:
            stats.errors += 1
            logger.error("Failed to update note {}: {}", note_id, exc)


def _do_fill(
    deck: str,
    prompt: str,
    target_fields: str,
    config: str | None = None,
    force: bool = False,
    allow_partial_source: bool = False,
    delay: float = 0.0,
    dry_run: bool = False,
    model_name: str | None = None,
    api_base: str | None = None,
    count: int = 0,
    batch: int = 1,
    raw_prompt: bool = False,
) -> None:
    app_cfg = load_config()
    model_cfg = app_cfg.resolve(config)
    tfields = parse_comma_list(target_fields)
    sfields = extract_source_fields(prompt)
    if not tfields:
        raise SystemExit("--target-fields must list at least one field")
    if not sfields:
        logger.warning("Prompt contains no {{Field}} placeholders")

    if not raw_prompt:
        import json

        json_template = json.dumps({f: "..." for f in tfields})
        prompt = f"{prompt}\nReturn JSON: {json_template}"

    logger.info("Prompt:\n{}", prompt)

    client = AnkiConnectClient()
    deck_name = resolve_deck_name(client, deck)
    note_ids = client.find_notes(f'deck:"{deck_name}"')
    if not note_ids:
        logger.info("No notes in deck {deck!r}", deck=deck_name)
        return

    notes = client.notes_info(note_ids)
    validate_target_fields(notes, tfields, sfields)
    total_notes = len(notes)

    # --- pre-scan: count eligible / skipped before starting ---
    skipped_source = 0
    skipped_target = 0
    for note in notes:
        fields = extract_field_values(note)
        if should_skip_source(fields, sfields, allow_partial=allow_partial_source):
            skipped_source += 1
        elif should_skip_target(fields, tfields, force=force):
            skipped_target += 1

    eligible = total_notes - skipped_source - skipped_target

    logger.info(
        "Deck {deck!r}: {total} notes, sources={sources}, targets={targets}, model={model}",
        deck=deck_name,
        total=total_notes,
        sources=sfields,
        targets=tfields,
        model=config or app_cfg.default_model,
    )
    logger.info(
        "Pre-scan: {eligible} eligible, {skipped_source} skipped (source empty), {skipped_target} skipped (target filled)",
        eligible=eligible,
        skipped_source=skipped_source,
        skipped_target=skipped_target,
    )

    if eligible == 0:
        logger.info("Nothing to do — all notes are already filled or missing source data.")
        return

    llm = LLMClient(model_cfg, model_name=model_name, api_base=api_base)
    stats = FillStats(total=total_notes, skipped=skipped_source + skipped_target)

    # Collect eligible notes
    eligible_notes: list[tuple[int, dict[str, str], dict[str, str]]] = []
    for note in notes:
        note_id = int(note["noteId"])
        fields = extract_field_values(note)
        skip = should_skip_source(fields, sfields, allow_partial=allow_partial_source)
        if skip is None:
            skip = should_skip_target(fields, tfields, force=force)
        if skip:
            continue
        source_vals = {name: fields.get(name, "") for name in sfields}
        eligible_notes.append((note_id, fields, source_vals))

    if count > 0:
        eligible_notes = eligible_notes[:count]

    if batch <= 0:
        batch = 1

    batch_idx = 0
    while batch_idx < len(eligible_notes):
        batch_notes = eligible_notes[batch_idx : batch_idx + batch]
        batch_idx += len(batch_notes)

        if batch == 1:
            # Sequential: simple single-note path
            note_id, fields, source_vals = batch_notes[0]
            compiled = replace_placeholders(prompt, source_vals)
            logger.log("DETAIL", "Note {} prompt:\n{}", note_id, compiled)
            try:
                raw = llm.complete(compiled)
            except RuntimeError as exc:
                stats.errors += 1
                logger.error("Note {}: {}", note_id, exc)
                continue
            _process_result(note_id, fields, raw, tfields, sfields, dry_run, stats, eligible, client)
        else:
            # Parallel batch
            from concurrent.futures import ThreadPoolExecutor, as_completed

            futures: dict = {}
            executor = ThreadPoolExecutor(max_workers=len(batch_notes))
            try:
                for note_id, fields, source_vals in batch_notes:
                    compiled = replace_placeholders(prompt, source_vals)
                    logger.log("DETAIL", "Note {} prompt:\n{}", note_id, compiled)
                    futures[executor.submit(llm.complete, compiled)] = (note_id, fields)

                for future in as_completed(futures):
                    note_id, fields = futures[future]
                    try:
                        raw = future.result()
                    except RuntimeError as exc:
                        stats.errors += 1
                        logger.error("Note {}: {}", note_id, exc)
                        continue
                    _process_result(note_id, fields, raw, tfields, sfields, dry_run, stats, eligible, client)
            except KeyboardInterrupt:
                for f in futures:
                    f.cancel()
                raise
            finally:
                executor.shutdown(wait=False, cancel_futures=True)

        if delay > 0:
            time.sleep(delay)

        # Note: count limit is handled by eligible_notes slicing above
        if count > 0 and len(eligible_notes) >= count and batch_idx >= count:
            logger.info("Reached count limit ({}) — stopping.", count)
            break

    logger.info(
        "Done. processed={processed} skipped={skipped} errors={errors} total={total}",
        **stats.__dict__,
    )


app = typer.Typer(no_args_is_help=True)
deck_app = typer.Typer(no_args_is_help=True)
model_app = typer.Typer(no_args_is_help=True)
app.add_typer(deck_app, name="deck", help="Deck operations")
app.add_typer(model_app, name="model", help="Manage LLM models")


@app.command()
def ping(
    check_key: bool = typer.Option(False, "--check-key", help="Verify API key is set for the selected model"),
    config: str | None = typer.Option(None, "-c", "--config", help="Model name from .ankiman_config.yaml"),
) -> None:
    """Test AnkiConnect connection."""
    _do_ping(check_key, config)


@deck_app.command(name="list")
def deck_list() -> None:
    """List decks with numbers."""
    _do_deck_list()


@deck_app.command(name="fields")
def deck_fields(
    deck: str = typer.Option(..., "-d", "--deck", help="Deck index or exact name"),
) -> None:
    """Show field names for a deck with example values from a real note.

    \b
    Example:
      ankiman deck fields -d 2
    """
    _do_deck_fields(deck)


@model_app.command(name="add")
def model_add(
    name: str = typer.Argument(help="Saved model name to use later with fill -c"),
    model: str = typer.Option(
        ...,
        "-m",
        "--model",
        help="LLM model name (e.g. deepseek-chat)",
    ),
    api_base: str = typer.Option(
        ...,
        "--api-base",
        help="API base URL (e.g. https://api.deepseek.com/v1)",
    ),
    api_key_env: str = typer.Option("", "--api-key-env", help="Env var for the API key (default: NAME_API_KEY derived from model name)"),
    set_default: bool = typer.Option(False, "--set-default", help="Set as default model"),
    force: bool = typer.Option(False, "--force", help="Overwrite existing model name"),
) -> None:
    """Add a saved LLM model to .ankiman_config.yaml.

    \b
    Example:
      ankiman model add deepseek --model deepseek-chat --api-base https://api.deepseek.com/v1 --set-default

    The env var for the API key is auto-derived from the model name (e.g. DEEPSEEK_API_KEY).
    Override with --api-key-env.
    """
    _do_model_add(name, model, api_base, api_key_env, set_default, force)


@model_app.command(name="list")
def model_list() -> None:
    """List all configured models."""
    app_cfg = load_config()
    for name, mc in app_cfg.models.items():
        default_mark = " [default]" if name == app_cfg.default_model else ""
        logger.info("{}{} model={} api_base={}", name, default_mark, mc.model, mc.api_base)
    if not app_cfg.models:
        logger.info("No models configured. Add one with: ankiman model add <name> ...")


@model_app.command(name="default")
def model_default(
    name: str = typer.Argument(help="Model name to set as default"),
) -> None:
    """Switch the active model (set as default in config)."""
    cfg_path = config_path()
    app_cfg = load_config(cfg_path)
    if name not in app_cfg.models:
        available = ", ".join(sorted(app_cfg.models)) or "(none)"
        raise SystemExit(f"Unknown model {name!r}. Available: {available}")
    app_cfg.default_model = name
    save_config(app_cfg, cfg_path)
    logger.info("Default model set to {name!r}", name=name)


@model_app.command(name="balance")
def model_balance(
    config: str | None = typer.Option(None, "-c", "--config", help="Model name from .ankiman_config.yaml (default: config default)"),
) -> None:
    """Check API account balance for the configured model."""
    app_cfg = load_config()
    model_cfg = app_cfg.resolve(config)
    try:
        balances = check_balance(model_cfg)
    except RuntimeError as exc:
        raise SystemExit(str(exc))
    logger.info("Balance for {name!r}:", name=config or app_cfg.default_model)
    for currency, info in balances.items():
        logger.info("  {}: {}", currency, info)


@app.command()
def fill(
    deck: str = typer.Option(..., "-d", "--deck", help="Deck index or exact name"),
    prompt: str = typer.Option(..., "-p", "--prompt", help="Prompt template with {Field} placeholders"),
    target_fields: str = typer.Option(..., "-t", "--target-fields", help="Comma-separated Anki field names to update"),
    config: str | None = typer.Option(None, "-c", "--config", help="Model name from .ankiman_config.yaml (default: config default)"),
    force: bool = typer.Option(False, "-f", "--force", help="Re-fill notes even when all target fields are already filled"),
    allow_partial_source: bool = typer.Option(False, "--allow-partial-source", help="Process notes when some (not all) source fields are filled"),
    delay: float = typer.Option(0.0, "-w", "--wait", help="Seconds between batches"),
    batch: int = typer.Option(1, "-b", "--batch", help="Parallel LLM calls per batch (1 = sequential)"),
    dry_run: bool = typer.Option(False, "-n", "--dry-run", help="Call LLM but do not update Anki"),
    model_name: str | None = typer.Option(None, "--model-name", help="Override API model string for this run"),
    api_base: str | None = typer.Option(None, "--api-base", help="Override API base URL for this run"),
    count: int = typer.Option(0, "-l", "--limit-count", help="Limit how many notes to process (0 = no limit)"),
    raw_prompt: bool = typer.Option(False, "--raw-prompt", help="Disable auto-generated JSON instruction in prompt"),
) -> None:
    """Fill target fields from LLM responses.

    \b
    Example:
      ankiman fill -d 2 \\
          -p 'Translate {Cantonese} to Mandarin' \\
          -t MandarinAnalogue

    The JSON format instruction is auto-generated from --target-fields.
    Use --raw-prompt to provide your own format instructions.

    Find deck numbers with: ankiman deck list
    Find field names with:      ankiman deck fields -d DECK
    """
    _do_fill(deck, prompt, target_fields, config, force, allow_partial_source, delay, dry_run, model_name, api_base, count, batch, raw_prompt)


def main() -> None:
    v_count = 0
    filtered = []
    for arg in sys.argv[1:]:
        if arg == "--verbose":
            v_count += 1
        elif arg.startswith("-v") and all(c == "v" for c in arg[2:]):
            v_count += len(arg) - 1  # -v=1, -vv=2, -vvv=3
        else:
            filtered.append(arg)
    sys.argv = [sys.argv[0]] + filtered
    logger.remove()

    try:
        logger.level("DETAIL", no=15, color="<cyan>")
    except Exception:
        pass

    if v_count == 0:
        level = "INFO"
    elif v_count == 1:
        level = "DETAIL"
    else:
        level = "DEBUG"

    logger.add(sys.stderr, level=level, format="<green>{time:HH:mm:ss}</green> <level>{level:<7}</level> {message}")
    signal.signal(signal.SIGINT, lambda _signum, _frame: os._exit(1))

    # Raise FD limit to avoid EMFILE under high concurrency.
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft < 4096:
            resource.setrlimit(resource.RLIMIT_NOFILE, (min(4096, hard), hard))
    except Exception:
        pass

    try:
        app()
    except AnkiConnectError as exc:
        logger.error("{}", exc)
    except KeyboardInterrupt:
        logger.warning("Interrupted by user")
        os._exit(1)
