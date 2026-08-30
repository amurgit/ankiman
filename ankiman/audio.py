from __future__ import annotations

import re
import time
from typing import Any

import structlog

from .anki import AnkiConnectClient, AnkiConnectError, extract_field_values, resolve_deck_name
from .note_filter import filter_notes, plain_field_text
from .tts import TTSError, resolve_voice, synthesize
from .util import FillStats, build_anki_query, extract_source_fields, parse_comma_list, replace_placeholders

logger = structlog.get_logger()

FILENAME_SAFE_RE = re.compile(r"[^\w.\-]+")


def audio_field_value(filename: str) -> str:
    return f"[sound:{filename}]"


def resolve_filename(template: str, fields: dict[str, str]) -> str:
    filename = replace_placeholders(template, fields).strip()
    filename = FILENAME_SAFE_RE.sub("_", filename)
    if not filename:
        raise ValueError("filename template resolved to empty string")
    return filename


def _validate_fields(notes: list[dict[str, Any]], required: list[str]) -> None:
    if not notes:
        return
    by_model: dict[str, set[str]] = {}
    for note in notes:
        model = str(note.get("modelName", "unknown"))
        by_model.setdefault(model, set()).update(extract_field_values(note).keys())
    for model, available in by_model.items():
        for field in required:
            if field not in available:
                raise SystemExit(
                    f"Field {field!r} does not exist on note type {model!r}.\n"
                    f"Create fields in the Anki app before running audio fill."
                )


def _audio_skip_reason(fields: dict[str, str], text_field: str, audio_field: str, *, force: bool) -> str | None:
    text = plain_field_text(fields.get(text_field, ""))
    if not text:
        return "empty_text"
    audio = plain_field_text(fields.get(audio_field, ""))
    if audio and not force:
        return "filled"
    return None


def _do_audio_fill(
    *,
    deck: str | None,
    tags: str | None,
    filter_expr: str | None,
    text_field: str,
    audio_field: str,
    filename_template: str,
    language: str,
    voice: str | None,
    force: bool,
    dry_run: bool,
    tag_add: str | None,
    tag_delete: str | None,
    count: int,
    delay: float,
) -> None:
    if not deck and not tags:
        raise SystemExit("Specify at least --deck or --tags")

    tag_list = parse_comma_list(tags) if tags else None
    tag_add_list = parse_comma_list(tag_add) if tag_add else []
    tag_delete_list = parse_comma_list(tag_delete) if tag_delete else []
    lang, resolved_voice = resolve_voice(language=language, voice=voice)

    client = AnkiConnectClient()
    deck_name = resolve_deck_name(client, deck) if deck else None
    query = build_anki_query(deck_name, tag_list)
    note_ids = client.find_notes(query)
    if not note_ids:
        filters = [f"deck={deck_name or '?'!r}"]
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

    template_fields = extract_source_fields(filename_template)
    _validate_fields(notes, [text_field, audio_field, *template_fields])

    skipped_empty = 0
    skipped_filled = 0
    eligible_notes: list[tuple[int, dict[str, str]]] = []
    for note in notes:
        note_id = int(note["noteId"])
        fields = extract_field_values(note)
        reason = _audio_skip_reason(fields, text_field, audio_field, force=force)
        if reason == "empty_text":
            skipped_empty += 1
            continue
        if reason == "filled":
            skipped_filled += 1
            continue
        eligible_notes.append((note_id, fields))

    eligible = len(eligible_notes)
    logger.info(
        "Pre-scan",
        eligible=eligible,
        text_empty=skipped_empty,
        audio_filled=skipped_filled,
        language=lang,
        voice=resolved_voice,
        text_field=text_field,
        audio_field=audio_field,
    )
    if eligible == 0:
        logger.info("Nothing to do — use -f/--force to regenerate existing audio.")
        return

    if count > 0:
        eligible_notes = eligible_notes[:count]

    stats = FillStats(total=len(notes), skipped=skipped_empty + skipped_filled)
    for idx, (note_id, fields) in enumerate(eligible_notes, start=1):
        text = plain_field_text(fields.get(text_field, ""))
        try:
            filename = resolve_filename(filename_template, fields)
        except ValueError as exc:
            stats.errors += 1
            logger.error(f"Note {note_id}: {exc}")
            continue

        tag = "[DRY RUN] " if dry_run else ""
        logger.info(
            f"[{idx}/{eligible}] {tag}Note {note_id}",
            text=preview_short(text),
            filename=filename,
        )

        if dry_run:
            stats.processed += 1
            continue

        try:
            audio_bytes = synthesize(text, voice=resolved_voice)
            client.store_media_file(filename, audio_bytes)
            client.update_note_fields(note_id, {audio_field: audio_field_value(filename)})
            if tag_add_list:
                client.add_tags([note_id], tag_add_list)
            if tag_delete_list:
                client.remove_tags([note_id], tag_delete_list)
            stats.processed += 1
            logger.debug(f"Note {note_id}: wrote {len(audio_bytes)} bytes")
        except (TTSError, AnkiConnectError, ValueError) as exc:
            stats.errors += 1
            logger.error(f"Note {note_id}: {exc}")

        if delay > 0 and idx < len(eligible_notes):
            time.sleep(delay)

    logger.info(
        "Done",
        processed=stats.processed,
        skipped=stats.skipped,
        errors=stats.errors,
        total=stats.total,
    )


def preview_short(text: str, limit: int = 40) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _do_audio_test(
    text: str,
    *,
    language: str,
    voice: str | None,
    output_path,
    play: bool,
) -> None:
    from pathlib import Path

    from .util import play_audio_file

    _lang, resolved_voice = resolve_voice(language=language, voice=voice)
    path = Path(output_path)
    audio = synthesize(text, voice=resolved_voice)
    path.write_bytes(audio)
    logger.info(f"Wrote {len(audio)} bytes to {path}")
    if play:
        play_audio_file(path)
        logger.info("Playback finished")
