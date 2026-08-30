from __future__ import annotations

import logging
import os
import resource
import signal
import sys
import time
from pathlib import Path
from typing import Any

import structlog
import typer

from .anki import (
    AnkiConnectClient,
    AnkiConnectError,
    extract_field_values,
    preview_field_text,
    resolve_deck_name,
)
from .audio import _do_audio_fill, _do_audio_test
from .config import AppConfig, ModelConfig, config_path, default_env_var, ensure_api_key, load_config, reset_config, save_config
from .secrets import delete_secret, secret_backend_name, set_secret
from .note_filter import compile_note_filter, filter_notes, note_matches, resolve_show_fields
from .llm import LLMClient, check_balance, parse_ai_response, verify_api_access
from .tts import DEFAULT_VOICES, LANGUAGE_ALIASES, language_choices, resolve_voice
from .util import FillStats, build_anki_query, extract_source_fields, parse_comma_list, replace_placeholders

logger = structlog.get_logger()


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


def _do_ping(check_key: bool, config: str | None) -> None:
    client = AnkiConnectClient()
    ver = client.version()
    decks = client.deck_names()
    logger.info(f"AnkiConnect OK (version {ver}) — {len(decks)} deck(s)")
    if check_key:
        app_cfg = load_config()
        model_cfg = app_cfg.resolve(config)
        key = ensure_api_key(model_cfg.api_key_env, prompt=False)
        logger.info(f"API key {model_cfg.api_key_env} is set ({key[:4]}…)")


def _do_deck_list() -> None:
    client = AnkiConnectClient()
    names = sorted(client.deck_names())
    logger.info(f"{'#':>3}  {'Deck':<40}  Notes")
    for i, name in enumerate(names, start=1):
        count = len(client.find_notes(f'deck:"{name}"'))
        logger.info(f"{i:>3}  {name:<40}  {count}")


def _do_deck_fields(deck: str) -> None:
    client = AnkiConnectClient()
    deck_name = resolve_deck_name(client, deck)
    note_ids = client.find_notes(f'deck:"{deck_name}"')
    if not note_ids:
        logger.info(f"No notes in deck {deck_name!r}")
        return

    notes = client.notes_info(note_ids)
    by_model: dict[str, dict[str, Any]] = {}
    for note in notes:
        model = str(note.get("modelName", "unknown"))
        fields = extract_field_values(note)
        entry = by_model.setdefault(model, {"fields": set(), "example": fields})
        entry["fields"].update(fields.keys())

    logger.info(
        f"Deck {deck_name!r}: {len(notes)} note(s), {len(by_model)} note type(s)"
    )
    logger.info(f"Use these field names in prompt placeholders like {{Field}} and in --target-fields.")

    for model in sorted(by_model):
        info = by_model[model]
        example_fields: dict[str, str] = info["example"]
        logger.info(f"Note type: {model}")
        for field_name in sorted(info["fields"]):
            logger.info(
                f"  {field_name:<24} {preview_field_text(example_fields.get(field_name, ''))}"
            )


def _prompt_model_add(
    name: str | None,
    model: str | None,
    api_base: str | None,
    api_key: str | None,
    api_key_env: str,
    set_default: bool | None,
    force: bool,
) -> tuple[str, str, str, str, str, bool, bool]:
    cfg_path = config_path()
    if cfg_path.is_file():
        app_cfg = load_config(cfg_path)
    else:
        app_cfg = AppConfig(default_model="", models={})

    interactive = name is None or model is None or api_base is None or api_key is None

    if interactive:
        typer.echo("Add a new LLM model to ankiman.\n")

    if name is None:
        name = typer.prompt("Config name (used with fill -c)").strip()
    if not name:
        raise SystemExit("Model name cannot be empty.")

    if name in app_cfg.models and not force:
        if interactive:
            if not typer.confirm(f"Model {name!r} already exists. Overwrite?", default=False):
                raise SystemExit("Aborted.")
            force = True
        else:
            raise SystemExit(f"Model {name!r} already exists. Use --force to overwrite.")

    if model is None:
        model = typer.prompt("API model name (e.g. deepseek-chat)").strip()
    if not model:
        raise SystemExit("API model name cannot be empty.")

    if api_base is None:
        api_base = typer.prompt("API base URL (e.g. https://api.deepseek.com/v1)").strip().rstrip("/")
    if not api_base:
        raise SystemExit("API base URL cannot be empty.")

    if not api_key_env:
        api_key_env = default_env_var(name)
        if interactive:
            typer.echo(f"API key will be stored as {api_key_env!r} in {secret_backend_name()}.")

    if api_key is None:
        api_key = typer.prompt("API key", hide_input=True).strip()
    if not api_key:
        raise SystemExit("API key cannot be empty.")

    if set_default is None:
        if not app_cfg.models:
            set_default = True
        elif interactive:
            set_default = typer.confirm("Set as default model?", default=True)
        else:
            set_default = False

    return name, model, api_base.rstrip("/"), api_key, api_key_env, set_default, force


def _do_model_add(
    name: str | None = None,
    model: str | None = None,
    api_base: str | None = None,
    api_key: str | None = None,
    api_key_env: str = "",
    set_default: bool | None = None,
    force: bool = False,
    skip_check: bool = False,
) -> None:
    name, model, api_base, api_key, api_key_env, set_default, force = _prompt_model_add(
        name, model, api_base, api_key, api_key_env, set_default, force
    )

    if not skip_check:
        logger.info("Verifying API key…")
        try:
            verify_api_access(api_key=api_key, api_base=api_base, model=model)
        except RuntimeError as exc:
            raise SystemExit(f"API key verification failed: {exc}")
        logger.info("API key verified")

    cfg_path = config_path()
    if cfg_path.is_file():
        app_cfg = load_config(cfg_path)
    else:
        app_cfg = AppConfig(default_model=name, models={})

    app_cfg.models[name] = ModelConfig(
        name=name,
        model=model,
        api_base=api_base,
        api_key_env=api_key_env,
    )
    if set_default:
        app_cfg.default_model = name
    save_config(app_cfg, cfg_path)
    set_secret(api_key_env, api_key)
    logger.info(
        f"Saved model to {cfg_path}",
        name=name,
        model=model,
        api_base=api_base,
        api_key_ref=api_key_env,
        api_key_store=secret_backend_name(),
    )
    if set_default:
        logger.info(f"Default model set to {name!r}")


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
    *,
    validate_filter: Any = None,
    tag_add: list[str] | None = None,
    tag_delete: list[str] | None = None,
) -> None:
    logger.debug(f"Note {note_id} raw response:\n{raw}")
    try:
        updates = parse_ai_response(raw, tfields)
    except ValueError as exc:
        stats.errors += 1
        logger.error(f"Note {note_id}: {exc}")
        return

    if validate_filter is not None:
        merged = {**fields, **updates}
        if not note_matches(validate_filter, merged):
            stats.errors += 1
            logger.error(f"Note {note_id}: validation failed (--validate-filter)")
            return

    stats.processed += 1

    tag = "[DRY RUN] " if dry_run else ""
    counter = f"[{stats.processed}/{eligible}]"
    source_kwargs = {name: preview_field_text(fields.get(name, '')) for name in sfields}
    target_kwargs = {f"→{name}": preview_field_text(updates.get(name, '')) for name in tfields}
    logger.info(f"{counter} {tag}", **source_kwargs, **target_kwargs)

    if dry_run:
        return

    try:
        client.update_note_fields(note_id, updates)
        if tag_add:
            client.add_tags([note_id], tag_add)
        if tag_delete:
            client.remove_tags([note_id], tag_delete)
    except AnkiConnectError as exc:
        stats.errors += 1
        logger.error(f"Failed to update note {note_id}: {exc}")


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
    tags: str | None = None,
    filter_expr: str | None = None,
    tag_add: str | None = None,
    tag_delete: str | None = None,
    validate_filter_expr: str | None = None,
) -> None:
    app_cfg = load_config()
    model_cfg = app_cfg.resolve(config)
    tfields = parse_comma_list(target_fields)
    sfields = extract_source_fields(prompt)
    tag_list = parse_comma_list(tags) if tags else None
    tag_add_list = parse_comma_list(tag_add) if tag_add else []
    tag_delete_list = parse_comma_list(tag_delete) if tag_delete else []
    validate_filter = compile_note_filter(validate_filter_expr) if validate_filter_expr else None
    if not tfields:
        raise SystemExit("--target-fields must list at least one field")
    if not sfields:
        logger.warning("Prompt contains no {Field} placeholders")

    if not raw_prompt:
        import json

        json_template = json.dumps({f: "..." for f in tfields})
        prompt = f"{prompt}\nReturn JSON: {json_template}"

    logger.info(f"Prompt:\n{prompt!r}")

    client = AnkiConnectClient()
    deck_name = resolve_deck_name(client, deck)
    query = build_anki_query(deck_name, tag_list)
    note_ids = client.find_notes(query)
    if not note_ids:
        filters = [f"deck={deck_name!r}"]
        if tag_list:
            filters.append(f"tags={tag_list}")
        logger.info("No notes found", filter=", ".join(filters))
        return

    notes = client.notes_info(note_ids)
    total_before_filter = len(notes)
    if filter_expr:
        notes = filter_notes(notes, filter_expr)
        logger.info(f"Filter matched {len(notes)}/{total_before_filter} note(s)")

    if not notes:
        logger.info("No notes matched filter")
        return

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
        f"Deck {deck_name!r}",
        notes=total_notes,
        sources=sfields,
        targets=tfields,
        model=config or app_cfg.default_model,
        tags=tag_list or [],
    )
    logger.info(
        "Pre-scan",
        eligible=eligible,
        source_empty=skipped_source,
        target_filled=skipped_target,
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
            logger.debug(f"Note {note_id} prompt:\n{compiled}")
            try:
                raw = llm.complete(compiled)
            except RuntimeError as exc:
                stats.errors += 1
                logger.error(f"Note {note_id}: {exc}")
                continue
            _process_result(
                note_id, fields, raw, tfields, sfields, dry_run, stats, eligible, client,
                validate_filter=validate_filter,
                tag_add=tag_add_list,
                tag_delete=tag_delete_list,
            )
        else:
            # Parallel batch
            from concurrent.futures import ThreadPoolExecutor, as_completed

            futures: dict = {}
            executor = ThreadPoolExecutor(max_workers=len(batch_notes))
            try:
                for note_id, fields, source_vals in batch_notes:
                    compiled = replace_placeholders(prompt, source_vals)
                    logger.debug(f"Note {note_id} prompt:\n{compiled}")
                    futures[executor.submit(llm.complete, compiled)] = (note_id, fields)

                for future in as_completed(futures):
                    note_id, fields = futures[future]
                    try:
                        raw = future.result()
                    except RuntimeError as exc:
                        stats.errors += 1
                        logger.error(f"Note {note_id}: {exc}")
                        continue
                    _process_result(
                note_id, fields, raw, tfields, sfields, dry_run, stats, eligible, client,
                validate_filter=validate_filter,
                tag_add=tag_add_list,
                tag_delete=tag_delete_list,
            )
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
            logger.info("Count limit reached — stopping", limit=count)
            break

    logger.info(
        "Done",
        processed=stats.processed,
        skipped=stats.skipped,
        errors=stats.errors,
        total=stats.total,
    )


app = typer.Typer(no_args_is_help=True)
config_app = typer.Typer(no_args_is_help=True)
deck_app = typer.Typer(no_args_is_help=True)
model_app = typer.Typer(no_args_is_help=True)
audio_app = typer.Typer(no_args_is_help=True)
app.add_typer(config_app, name="config", help="Manage ankiman config")
app.add_typer(deck_app, name="deck", help="Deck operations")
app.add_typer(model_app, name="model", help="Manage LLM models")
app.add_typer(audio_app, name="audio", help="Generate audio with edge-tts")


@app.command()
def ping(
    check_key: bool = typer.Option(False, "--check-key", help="Verify API key is set for the selected model"),
    config: str | None = typer.Option(None, "-c", "--config", help="Model name from .ankiman_config.yaml"),
) -> None:
    """Test AnkiConnect connection."""
    _do_ping(check_key, config)


@config_app.command(name="reset")
def config_reset(
    yes: bool = typer.Option(False, "-y", "--yes", help="Skip confirmation"),
    keys: bool = typer.Option(False, "--keys", help="Also delete stored API keys for configured models"),
) -> None:
    """Remove .ankiman_config.yaml (for dev/debugging).

    \b
    Example:
      ankiman config reset -y
      ankiman config reset -y --keys
    """
    cfg_path = config_path()
    if not cfg_path.is_file():
        logger.info(f"No config at {cfg_path}")
        return

    prompt = f"Delete {cfg_path.name}"
    if keys:
        prompt += " and stored API keys"
    prompt += "?"

    if not yes and not typer.confirm(prompt, default=False):
        raise SystemExit("Aborted.")

    key_refs = reset_config(delete_keys=keys)
    logger.info(f"Removed {cfg_path}")

    if keys:
        removed = 0
        for ref in key_refs:
            if delete_secret(ref):
                removed += 1
                logger.info(f"Removed API key {ref!r} from {secret_backend_name()}")
        if key_refs and removed == 0:
            logger.info("No stored API keys found to remove")


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
    name: str | None = typer.Argument(None, help="Saved model name to use later with fill -c"),
    model: str | None = typer.Option(
        None,
        "-m",
        "--model",
        help="LLM model name (e.g. deepseek-chat)",
    ),
    api_base: str | None = typer.Option(
        None,
        "--api-base",
        help="API base URL (e.g. https://api.deepseek.com/v1)",
    ),
    api_key: str | None = typer.Option(None, "--api-key", help="API key (prompted if omitted)"),
    api_key_env: str = typer.Option("", "--api-key-env", help="Secret key name (default: NAME_API_KEY)"),
    set_default: bool | None = typer.Option(None, "--set-default/--no-set-default", help="Set as default model"),
    force: bool = typer.Option(False, "--force", help="Overwrite existing model name"),
    skip_check: bool = typer.Option(False, "--skip-check", help="Skip API key verification"),
) -> None:
    """Add a saved LLM model interactively or from flags.

    \b
    Interactive (prompts for each value):
      ankiman model add

    \b
    Non-interactive:
      ankiman model add deepseek --model deepseek-chat --api-base https://api.deepseek.com/v1 --api-key sk-... --set-default

    Verifies the API key with a minimal completion request before saving.
    On macOS the API key is stored in Keychain; on other platforms it is saved to .env.
    """
    _do_model_add(name, model, api_base, api_key, api_key_env, set_default, force, skip_check)


@model_app.command(name="list")
def model_list() -> None:
    """List all configured models."""
    app_cfg = load_config()
    for name, mc in app_cfg.models.items():
        default_mark = " *" if name == app_cfg.default_model else ""
        logger.info(
            f"{name}{default_mark}",
            model=mc.model,
            api_base=mc.api_base,
        )
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
    logger.info(f"Default model set to {name!r}")


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
    logger.info(f"Balance for {config or app_cfg.default_model!r}", **balances)


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
    raw_prompt: bool = typer.Option(False, "-r", "--raw-prompt", help="Disable auto-generated JSON instruction in prompt"),
    tags: str | None = typer.Option(None, "-g", "--tags", help="Filter by tags (comma-separated, OR logic)"),
    filter_expr: str | None = typer.Option(
        None,
        "-F",
        "--filter",
        help="Jinja filter on field values (applied before LLM)",
    ),
    tag_add: str | None = typer.Option(None, "-ta", "--tag-add", help="Add tag(s) after successful write (comma-separated)"),
    tag_delete: str | None = typer.Option(None, "-td", "--tag-delete", help="Remove tag(s) after successful write"),
    validate_filter_expr: str | None = typer.Option(
        None,
        "--validate-filter",
        help="Jinja filter on merged fields after LLM; reject update if false",
    ),
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
    _do_fill(
        deck, prompt, target_fields, config, force, allow_partial_source, delay, dry_run,
        model_name, api_base, count, batch, raw_prompt, tags, filter_expr,
        tag_add, tag_delete, validate_filter_expr,
    )


@app.command()
def show(
    deck: str | None = typer.Option(None, "-d", "--deck", help="Deck index or exact name"),
    tags: str | None = typer.Option(None, "-g", "--tags", help="Filter by tags (comma-separated, OR logic)"),
    filter_expr: str | None = typer.Option(
        None,
        "-F",
        "--filter",
        help='Jinja filter on field values, e.g. \'cantonese is empty or word == sentence\'',
    ),
    fields: str | None = typer.Option(
        None,
        "--fields",
        help="Fields to display (comma-separated). With -F, defaults to filter fields; prefix + to add",
    ),
    count: int = typer.Option(0, "-l", "--limit-count", help="Limit how many notes to show (0 = no limit)"),
    full: bool = typer.Option(False, "-f", "--full", help="Show full field content (not truncated)"),
) -> None:
    """Show notes with their field values and tags.

    \b
    Examples:
      ankiman show -d 2 -l 5
      ankiman show -g "to_review" -l 10
      ankiman show -d 3 -g "important" --full
      ankiman show -d 2 -F 'cantonese is empty'
      ankiman show -d 2 -F 'word in sentence' --fields +Audio,+Key
      ankiman show -d 3 -F 'Cantonese is not split_word_in(SentenceCantonese)' --fields +Jyutping

    With -F, only fields referenced in the filter are shown by default.
    Use --fields to override, or prefix with + to add fields (e.g. --fields +Audio,+Key).

    Field names are case-insensitive. Custom tests: empty, split_word_in (chars in order, gaps ok).
    """
    client = AnkiConnectClient()
    tag_list = parse_comma_list(tags) if tags else None
    deck_name = resolve_deck_name(client, deck) if deck else None

    if not deck and not tag_list:
        raise SystemExit("Specify at least --deck or --tags")

    query = build_anki_query(deck_name, tag_list)
    note_ids = client.find_notes(query)

    if not note_ids:
        filters = [f"deck={deck_name or '?'!r}"]
        if tag_list:
            filters.append(f"tags={tag_list}")
        logger.info("No notes found", filter=", ".join(filters))
        return

    notes = client.notes_info(note_ids)
    total = len(notes)

    if filter_expr:
        notes = filter_notes(notes, filter_expr)
        logger.info(f"Filter matched {len(notes)}/{total} note(s)")

    if not notes:
        logger.info("No notes matched filter")
        return

    if count > 0:
        notes = notes[:count]

    available_fields: set[str] = set()
    for note in notes:
        available_fields.update(extract_field_values(note).keys())
    show_fields = resolve_show_fields(fields, filter_expr, sorted(available_fields))

    logger.info(f"Showing {len(notes)} note(s)", fields=show_fields)

    for note in notes:
        note_id = note["noteId"]
        fields_map = extract_field_values(note)
        note_tags = note.get("tags", [])
        model = note.get("modelName", "?")

        display_fields: dict[str, str] = {}
        for name in show_fields:
            value = fields_map.get(name, "")
            display_fields[name] = value if full else preview_field_text(value)

        tag_str = f" [{', '.join(note_tags)}]" if note_tags else ""
        logger.info(f"Note {note_id} ({model}){tag_str}", **display_fields)


_LANG_HELP = f"TTS language ({', '.join(language_choices())})"


@audio_app.command(name="languages")
def audio_languages() -> None:
    """List supported --language values and default edge-tts voices."""
    by_canonical: dict[str, list[str]] = {}
    for alias, canonical in LANGUAGE_ALIASES.items():
        by_canonical.setdefault(canonical, []).append(alias)
    for canonical in sorted(by_canonical):
        aliases = ", ".join(sorted(by_canonical[canonical]))
        logger.info(canonical, aliases=aliases, default_voice=DEFAULT_VOICES[canonical])


@audio_app.command(name="fill")
def audio_fill(
    language: str = typer.Option(..., "-L", "--language", help=_LANG_HELP),
    deck: str | None = typer.Option(None, "-d", "--deck", help="Deck index or exact name"),
    tags: str | None = typer.Option(None, "-g", "--tags", help="Filter by tags (comma-separated, OR logic)"),
    filter_expr: str | None = typer.Option(None, "-F", "--filter", help="Jinja filter on field values"),
    text_field: str = typer.Option("Cantonese", "--text-field", help="Field with text to synthesize"),
    audio_field: str = typer.Option("Audio", "--field", help="Anki field to store [sound:...] reference"),
    filename_template: str = typer.Option(
        "canto_word_{Key}.mp3",
        "--filename-template",
        help="Media filename template ({Field} placeholders)",
    ),
    voice: str | None = typer.Option(None, "--voice", help="Override default edge-tts voice for this language"),
    force: bool = typer.Option(False, "-f", "--force", help="Regenerate even when audio field is filled"),
    dry_run: bool = typer.Option(False, "-n", "--dry-run", help="Preview without writing to Anki"),
    tag_add: str | None = typer.Option(None, "-ta", "--tag-add", help="Add tag(s) after successful write"),
    tag_delete: str | None = typer.Option(None, "-td", "--tag-delete", help="Remove tag(s) after successful write"),
    count: int = typer.Option(0, "-l", "--limit-count", help="Limit how many notes to process (0 = no limit)"),
    delay: float = typer.Option(0.0, "-w", "--wait", help="Seconds between notes"),
) -> None:
    """Generate MP3 via edge-tts and update Anki media + audio field.

    \b
    Examples:
      ankiman audio test 落雨 --language cantonese --play
      ankiman audio fill --language cantonese -g audio-pending -td audio-pending
      ankiman audio fill --language mandarin --text-field MandarinAnalogue -l 5 -n
    """
    _do_audio_fill(
        deck=deck,
        tags=tags,
        filter_expr=filter_expr,
        text_field=text_field,
        audio_field=audio_field,
        filename_template=filename_template,
        language=language,
        voice=voice,
        force=force,
        dry_run=dry_run,
        tag_add=tag_add,
        tag_delete=tag_delete,
        count=count,
        delay=delay,
    )


@audio_app.command(name="test")
def audio_test(
    text: str = typer.Argument(help="Text to synthesize"),
    language: str = typer.Option(..., "-L", "--language", help=_LANG_HELP),
    voice: str | None = typer.Option(None, "--voice", help="Override default edge-tts voice for this language"),
    output: Path = typer.Option(Path("test-output.mp3"), "-o", "--output", help="Output MP3 path"),
    play: bool = typer.Option(False, "--play", help="Play the MP3 after synthesis (afplay on macOS)"),
) -> None:
    """Synthesize one phrase to a file (no Anki write).

    \b
    Examples:
      ankiman audio test 你好 --language mandarin --play
      ankiman audio test 落雨 --language cantonese --play
      ankiman audio test hello --language english --voice en-US-GuyNeural
    """
    lang, resolved_voice = resolve_voice(language=language, voice=voice)
    logger.info("Synthesizing", language=lang, voice=resolved_voice, text=text)
    _do_audio_test(text, language=language, voice=voice, output_path=output, play=play)


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

    if v_count == 0:
        min_level = logging.INFO
    else:
        min_level = logging.DEBUG

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.dev.set_exc_info,
        structlog.processors.TimeStamper(fmt="%H:%M:%S", utc=False),
        structlog.dev.ConsoleRenderer(colors=True),
    ]

    structlog.configure(
        processors=shared_processors,
        wrapper_class=structlog.make_filtering_bound_logger(min_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )

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
        logger.error(str(exc))
    except KeyboardInterrupt:
        logger.warning("Interrupted by user")
        os._exit(1)
