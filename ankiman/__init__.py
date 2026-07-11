#!/usr/bin/env python3
"""ankiman — bulk-fill Anki note fields using an LLM via AnkiConnect.

Usage
-----
Setup::

    uv sync
    ankiman profile add deepseek -n deepseek-chat --api-base https://api.deepseek.com/v1 --set-default

List decks::

    ankiman deck list

Fill target fields from source placeholders in a prompt::

    ankiman fill -d 2 \\
        -p "Translate {Traditional} to Mandarin. Return JSON: {\\"Mandarin_Word\\": \\"...\\"}" \\
        -t Mandarin_Word \\
        -c deepseek

Configuration lives in ``.ankiman_config.yaml`` (``profiles:`` section).  API keys are
read from ``.env`` via python-dotenv (prompted on first use if missing).

Requires Anki running with the AnkiConnect add-on (http://localhost:8765).
"""

from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass
from getpass import getpass
from pathlib import Path
from typing import Any

import requests
import yaml
from dotenv import load_dotenv, set_key
from loguru import logger

ANKI_CONNECT_URL = "http://localhost:8765"
ANKI_CONNECT_VERSION = 6
CONFIG_FILENAME = ".ankiman_config.yaml"
ENV_FILENAME = ".env"
PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")
JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```\s*$", re.DOTALL | re.IGNORECASE)
NOTES_INFO_BATCH = 100
MAX_API_RETRIES = 3


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class ProfileConfig:
    """One entry under ``profiles:`` in the YAML config."""

    name: str
    model: str
    api_base: str
    api_key_env: str = "ANKI_LLM_API_KEY"


@dataclass
class AppConfig:
    default_profile: str
    profiles: dict[str, ProfileConfig]

    def resolve(self, name: str | None) -> ProfileConfig:
        key = name or self.default_profile
        if key not in self.profiles:
            available = ", ".join(sorted(self.profiles)) or "(none)"
            raise SystemExit(
                f"Unknown profile {key!r}. Available: {available}. "
                f"Add one with: ankiman profile add <name> ..."
            )
        return self.profiles[key]


def config_path(cwd: Path | None = None) -> Path:
    return (cwd or Path.cwd()) / CONFIG_FILENAME


def load_config(path: Path | None = None) -> AppConfig:
    path = path or config_path()
    if not path.is_file():
        raise SystemExit(
            f"Config not found: {path}\n"
            f"Create one with: ankiman profile add <name> -n <api-model> --api-base <url>"
        )
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    default = raw.get("default")
    profiles_raw = raw.get("profiles") or {}
    if not default:
        raise SystemExit(f"Missing 'default' in {path}")
    if not profiles_raw:
        raise SystemExit(f"Missing 'profiles' in {path}")
    profiles: dict[str, ProfileConfig] = {}
    for name, entry in profiles_raw.items():
        if not isinstance(entry, dict):
            raise SystemExit(f"Invalid profile entry {name!r} in {path}")
        for field in ("model", "api_base"):
            if field not in entry:
                raise SystemExit(f"Profile {name!r} missing required field {field!r}")
        profiles[name] = ProfileConfig(
            name=name,
            model=str(entry["model"]),
            api_base=str(entry["api_base"]).rstrip("/"),
            api_key_env=str(entry.get("api_key_env", "ANKI_LLM_API_KEY")),
        )
    return AppConfig(default_profile=str(default), profiles=profiles)


def save_config(app: AppConfig, path: Path | None = None) -> None:
    path = path or config_path()
    data: dict[str, Any] = {
        "default": app.default_profile,
        "profiles": {
            name: {
                "model": pc.model,
                "api_base": pc.api_base,
                "api_key_env": pc.api_key_env,
            }
            for name, pc in app.profiles.items()
        },
    }
    path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Environment / API keys
# ---------------------------------------------------------------------------


def env_path(cwd: Path | None = None) -> Path:
    return (cwd or Path.cwd()) / ENV_FILENAME


def ensure_api_key(env_var: str, *, prompt: bool = True) -> str:
    load_dotenv(env_path())
    import os

    key = os.environ.get(env_var, "").strip()
    if key:
        return key
    if not prompt:
        raise SystemExit(f"Environment variable {env_var} is not set.")
    logger.info(f"{env_var} not found — enter it now (saved to {ENV_FILENAME})")
    key = getpass(f"{env_var}: ").strip()
    if not key:
        raise SystemExit(f"Empty API key for {env_var}.")
    dotenv_file = env_path()
    if not dotenv_file.is_file():
        dotenv_file.write_text("", encoding="utf-8")
    set_key(str(dotenv_file), env_var, key)
    load_dotenv(dotenv_file, override=True)
    return key


# ---------------------------------------------------------------------------
# AnkiConnect
# ---------------------------------------------------------------------------


class AnkiConnectError(Exception):
    pass


class AnkiConnectClient:
    def __init__(self, url: str = ANKI_CONNECT_URL, api_version: int = ANKI_CONNECT_VERSION) -> None:
        self.url = url
        self.api_version = api_version

    def invoke(self, action: str, **params: Any) -> Any:
        payload: dict[str, Any] = {"action": action, "version": self.api_version}
        if params:
            payload["params"] = params
        logger.debug("AnkiConnect {} params={}", action, params)
        try:
            resp = requests.post(self.url, json=payload, timeout=60)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise AnkiConnectError(
                f"Cannot reach AnkiConnect at {self.url}. Is Anki running with the add-on?"
            ) from exc
        body = resp.json()
        if body.get("error"):
            raise AnkiConnectError(str(body["error"]))
        return body.get("result")

    def version(self) -> int:
        return int(self.invoke("version"))

    def deck_names(self) -> list[str]:
        return list(self.invoke("deckNames"))

    def find_notes(self, query: str) -> list[int]:
        return list(self.invoke("findNotes", query=query))

    def notes_info(self, note_ids: list[int]) -> list[dict[str, Any]]:
        if not note_ids:
            return []
        results: list[dict[str, Any]] = []
        for i in range(0, len(note_ids), NOTES_INFO_BATCH):
            batch = note_ids[i : i + NOTES_INFO_BATCH]
            results.extend(self.invoke("notesInfo", notes=batch))
        return results

    def update_note_fields(self, note_id: int, fields: dict[str, str]) -> None:
        self.invoke("updateNoteFields", note={"id": note_id, "fields": fields})


def resolve_deck_name(client: AnkiConnectClient, deck_arg: str) -> str:
    if deck_arg.isdigit():
        index = int(deck_arg)
        names = sorted(client.deck_names())
        if index < 1 or index > len(names):
            raise SystemExit(
                f"Deck index {index} out of range (1–{len(names)}). Run: ankiman deck list"
            )
        return names[index - 1]
    names = client.deck_names()
    if deck_arg not in names:
        raise SystemExit(f"Deck {deck_arg!r} not found. Run: ankiman deck list")
    return deck_arg


def extract_field_values(note: dict[str, Any]) -> dict[str, str]:
    fields_raw = note.get("fields") or {}
    return {name: (info.get("value") or "") for name, info in fields_raw.items()}


# ---------------------------------------------------------------------------
# Placeholders & skip logic
# ---------------------------------------------------------------------------


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
    return sum(1 for n in names if values.get(n, "").strip())


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
        empty = [n for n in source_fields if not fields.get(n, "").strip()]
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
    if all(fields.get(t, "").strip() for t in target_fields):
        return "all target fields already filled"
    return None


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------


def strip_json_fences(text: str) -> str:
    text = text.strip()
    match = JSON_FENCE_RE.match(text)
    if match:
        return match.group(1).strip()
    return text


def parse_ai_response(text: str, target_fields: list[str]) -> dict[str, str]:
    cleaned = strip_json_fences(text)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"JSON must be an object, got {type(data).__name__}")
    missing = [f for f in target_fields if f not in data]
    if missing:
        raise ValueError(f"JSON missing required key(s): {', '.join(missing)}")
    return {f: str(data[f]) for f in target_fields}


class LLMClient:
    def __init__(self, profile_cfg: ProfileConfig, *, model_name: str | None, api_base: str | None) -> None:
        self.model = model_name or profile_cfg.model
        self.api_base = (api_base or profile_cfg.api_base).rstrip("/")
        self.api_key = ensure_api_key(profile_cfg.api_key_env)

    def complete(self, prompt: str, *, delay: float) -> str:
        last_error: Exception | None = None
        for attempt in range(MAX_API_RETRIES):
            try:
                if attempt:
                    wait = delay * (2 ** (attempt - 1))
                    logger.debug("Retry {}/{} after {:.1f}s", attempt + 1, MAX_API_RETRIES, wait)
                    time.sleep(wait)
                return self._call(prompt)
            except Exception as exc:
                last_error = exc
                logger.debug("API attempt {} failed: {}", attempt + 1, exc)
        raise RuntimeError(f"API failed after {MAX_API_RETRIES} attempts: {last_error}") from last_error

    def _call(self, prompt: str) -> str:
        try:
            from openai import OpenAI  # noqa: PLC0415

            return self._call_openai(OpenAI)
        except ImportError:
            return self._call_requests()

    def _call_openai(self, OpenAI: type) -> str:
        client = OpenAI(api_key=self.api_key, base_url=self.api_base)
        response = client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
        )
        content = response.choices[0].message.content
        if not content:
            raise RuntimeError("empty response from API")
        return content

    def _call_requests(self, prompt: str) -> str:
        url = f"{self.api_base}/chat/completions"
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json={"model": self.model, "messages": [{"role": "user", "content": prompt}]},
            timeout=120,
        )
        if resp.status_code in (401, 403):
            resp.raise_for_status()
        if resp.status_code == 429 or resp.status_code >= 500:
            resp.raise_for_status()
        resp.raise_for_status()
        body = resp.json()
        try:
            return body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"unexpected API response shape: {body}") from exc


# ---------------------------------------------------------------------------
# Field validation
# ---------------------------------------------------------------------------


def validate_target_fields(
    notes: list[dict[str, Any]],
    target_fields: list[str],
    source_fields: list[str],
) -> None:
    if not notes:
        return
    by_model: dict[str, set[str]] = {}
    for note in notes:
        model = note.get("modelName", "unknown")
        by_model.setdefault(model, set()).update(extract_field_values(note).keys())
    for model, available in by_model.items():
        for field in target_fields + source_fields:
            if field not in available:
                raise SystemExit(
                    f"Field {field!r} does not exist on note type {model!r}.\n"
                    f"Create fields in the Anki app (Tools → Manage Note Types → Fields) "
                    f"before running fill."
                )


# ---------------------------------------------------------------------------
# Fill pipeline
# ---------------------------------------------------------------------------


@dataclass
class FillStats:
    total: int = 0
    processed: int = 0
    skipped: int = 0
    errors: int = 0


def preview_values(values: dict[str, str], limit: int = 80) -> str:
    parts = [f"{k}={v!r}" for k, v in values.items()]
    text = ", ".join(parts)
    return text if len(text) <= limit else text[: limit - 3] + "..."


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def _do_ping(check_key: bool, config: str | None) -> None:
    client = AnkiConnectClient()
    ver = client.version()
    decks = client.deck_names()
    logger.info("AnkiConnect OK (version {}) — {} deck(s)", ver, len(decks))
    if check_key:
        app_cfg = load_config()
        profile_cfg = app_cfg.resolve(config)
        key = ensure_api_key(profile_cfg.api_key_env, prompt=False)
        logger.info("API key {} is set ({}…)", profile_cfg.api_key_env, key[:4])


def _do_deck_list() -> None:
    client = AnkiConnectClient()
    names = sorted(client.deck_names())
    logger.info("{:>3}  {:<40}  Notes", "#", "Deck")
    for i, name in enumerate(names, start=1):
        count = len(client.find_notes(f'deck:"{name}"'))
        logger.info("{:>3}  {:<40}  {}", i, name, count)


def _do_profile_add(
    name: str,
    profile_name: str,
    api_base: str,
    api_key_env: str = "ANKI_LLM_API_KEY",
    set_default: bool = False,
    force: bool = False,
) -> None:
    path = config_path()
    if path.is_file():
        app_cfg = load_config(path)
    else:
        app_cfg = AppConfig(default_profile=name, profiles={})

    if name in app_cfg.profiles and not force:
        raise SystemExit(
            f"Profile {name!r} already exists. Use --force to overwrite."
        )

    app_cfg.profiles[name] = ProfileConfig(
        name=name,
        model=profile_name,
        api_base=api_base.rstrip("/"),
        api_key_env=api_key_env,
    )
    if set_default or not path.is_file():
        app_cfg.default_profile = name
    save_config(app_cfg, path)
    logger.info("Saved profile {name!r} to {path}", name=name, path=path)
    if set_default or not path.is_file():
        logger.info("Default profile set to {name!r}", name=name)


def _do_fill(
    deck: str,
    prompt: str,
    target_fields: str,
    config: str | None = None,
    force: bool = False,
    allow_partial_source: bool = False,
    delay: float = 0.3,
    dry_run: bool = False,
    model_name: str | None = None,
    api_base: str | None = None,
) -> None:
    app_cfg = load_config()
    profile_cfg = app_cfg.resolve(config)
    tfields = parse_comma_list(target_fields)
    sfields = extract_source_fields(prompt)
    if not tfields:
        raise SystemExit("--target-fields must list at least one field")
    if not sfields:
        logger.warning("Prompt contains no {{Field}} placeholders")

    client = AnkiConnectClient()
    deck_name = resolve_deck_name(client, deck)
    query = f'deck:"{deck_name}"'
    note_ids = client.find_notes(query)
    if not note_ids:
        logger.info('No notes in deck {deck!r}', deck=deck_name)
        return

    notes = client.notes_info(note_ids)
    validate_target_fields(notes, tfields, sfields)

    llm = LLMClient(profile_cfg, model_name=model_name, api_base=api_base)
    stats = FillStats(total=len(notes))
    to_process = stats.total

    logger.info(
        "Deck {deck!r}: {total} notes, sources={sources}, targets={targets}, profile={profile}",
        deck=deck_name,
        total=stats.total,
        sources=sfields,
        targets=tfields,
        profile=config or app_cfg.default_profile,
    )

    for idx, note in enumerate(notes, start=1):
        note_id = int(note["noteId"])
        fields = extract_field_values(note)

        skip = should_skip_source(fields, sfields, allow_partial=allow_partial_source)
        if skip is None:
            skip = should_skip_target(fields, tfields, force=force)
        if skip:
            stats.skipped += 1
            logger.warning("Skipping note {}: {}", note_id, skip)
            continue

        field_map = {name: fields.get(name, "") for name in sfields}
        compiled = replace_placeholders(prompt, field_map)
        logger.debug("Note {} prompt:\n{}", note_id, compiled)

        try:
            raw = llm.complete(compiled, delay=delay)
            logger.debug("Note {} raw response:\n{}", note_id, raw)
            updates = parse_ai_response(raw, tfields)
        except (RuntimeError, ValueError) as exc:
            stats.errors += 1
            logger.error("Note {}: {}", note_id, exc)
            continue

        source_preview = ", ".join(f"{k}={fields.get(k, '')!r}" for k in sfields[:2])
        logger.info(
            "[{}/{}] note={} {} -> {}",
            idx,
            to_process,
            note_id,
            source_preview,
            preview_values(updates),
        )

        if dry_run:
            logger.info("[DRY RUN] Would update note {}: {}", note_id, preview_values(updates))
        else:
            try:
                client.update_note_fields(note_id, updates)
                logger.info("Updated note {}: {}", note_id, preview_values(updates))
            except AnkiConnectError as exc:
                stats.errors += 1
                logger.error("Failed to update note {}: {}", note_id, exc)
                continue

        stats.processed += 1
        if delay > 0:
            time.sleep(delay)

    logger.info(
        "Done. processed={processed} skipped={skipped} errors={errors} total={total}",
        **stats.__dict__,
    )


# ---------------------------------------------------------------------------
# CLI (Typer)
# ---------------------------------------------------------------------------

import typer

app = typer.Typer(no_args_is_help=True)
deck_app = typer.Typer(no_args_is_help=True)
profile_app = typer.Typer(no_args_is_help=True)
app.add_typer(deck_app, name="deck", help="Deck operations")
app.add_typer(profile_app, name="profile", help="Manage LLM profiles")


@app.command()
def ping(
    check_key: bool = typer.Option(False, "--check-key", help="Verify API key is set for the selected profile"),
    config: str | None = typer.Option(None, "-c", "--config", help="Profile name from .ankiman_config.yaml"),
) -> None:
    """Test AnkiConnect connection."""
    _do_ping(check_key, config)


@deck_app.command(name="list")
def deck_list() -> None:
    """List decks with numbers."""
    _do_deck_list()


@profile_app.command(name="add")
def profile_add(
    name: str = typer.Argument(help="Profile name (used with fill -c)"),
    profile_name: str = typer.Option(..., "-n", "--profile-name", help="API model string (e.g. deepseek-chat)"),
    api_base: str = typer.Option(..., "--api-base", help="OpenAI-compatible base URL"),
    api_key_env: str = typer.Option("ANKI_LLM_API_KEY", "--api-key-env", help="Environment variable for the API key"),
    set_default: bool = typer.Option(False, "--set-default", help="Set as default profile"),
    force: bool = typer.Option(False, "--force", help="Overwrite existing profile name"),
) -> None:
    """Add a profile to .ankiman_config.yaml."""
    _do_profile_add(name, profile_name, api_base, api_key_env, set_default, force)


@app.command()
def fill(
    deck: str = typer.Option(..., "-d", "--deck", help="Deck index or exact name"),
    prompt: str = typer.Option(..., "-p", "--prompt", help="Prompt template with {Field} placeholders"),
    target_fields: str = typer.Option(..., "-t", "--target-fields", help="Comma-separated Anki field names to update"),
    config: str | None = typer.Option(None, "-c", "--config", help="Profile name from .ankiman_config.yaml (default: config default)"),
    force: bool = typer.Option(False, "-f", "--force", help="Re-fill notes even when all target fields are already filled"),
    allow_partial_source: bool = typer.Option(False, "--allow-partial-source", help="Process notes when some (not all) source fields are filled"),
    delay: float = typer.Option(0.3, "--delay", help="Seconds between API calls"),
    dry_run: bool = typer.Option(False, "-n", "--dry-run", help="Call LLM but do not update Anki"),
    model_name: str | None = typer.Option(None, "--model-name", help="Override API model string for this run"),
    api_base: str | None = typer.Option(None, "--api-base", help="Override API base URL for this run"),
) -> None:
    """Fill target fields from LLM responses."""
    _do_fill(deck, prompt, target_fields, config, force, allow_partial_source, delay, dry_run, model_name, api_base)


def main() -> None:
    verbose = "-v" in sys.argv or "--verbose" in sys.argv
    sys.argv = [a for a in sys.argv if a not in ("-v", "--verbose")]
    logger.remove()
    level = "DEBUG" if verbose else "INFO"
    logger.add(sys.stderr, level=level, format="<level>{level:<7}</level> {message}")
    app()


if __name__ == "__main__":
    main()
