from __future__ import annotations

import os
import re
import resource
from html import unescape
from typing import Any

import requests
import structlog

ANKI_CONNECT_URL = "http://localhost:8765"
ANKI_CONNECT_VERSION = 6
NOTES_INFO_BATCH = 100
HTML_BREAK_RE = re.compile(r"(?i)<br\s*/?>")
HTML_TAG_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"\s+")

logger = structlog.get_logger()


def _fmt_params(params: dict[str, Any]) -> str:
    """Format params for debug logging, truncating large lists."""
    result: dict[str, Any] = {}
    for key, value in params.items():
        if isinstance(value, list) and len(value) > 5:
            result[key] = f"[{len(value)} items]"
        else:
            result[key] = value
    return str(result)


class AnkiConnectError(Exception):
    pass


class AnkiConnectClient:
    def __init__(self, url: str = ANKI_CONNECT_URL, api_version: int = ANKI_CONNECT_VERSION) -> None:
        self.url = url
        self.api_version = api_version

    def invoke(self, action: str, **params: Any) -> Any:
        _log = params.pop("_log", True)
        payload: dict[str, Any] = {"action": action, "version": self.api_version}
        if params:
            payload["params"] = params
        if _log:
            logger.debug(f"AnkiConnect {action} params={_fmt_params(params)}")
        resp = None
        try:
            resp = requests.post(self.url, json=payload, timeout=60)
            resp.raise_for_status()
        except requests.RequestException as exc:
            if "Too many open files" in str(exc) or "Errno 24" in str(exc):
                try:
                    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
                    fd_count = len(os.listdir("/dev/fd"))
                    logger.warning(f"AnkiConnect FD diagnostic: open={fd_count} soft_limit={soft} hard_limit={hard}")
                except Exception:
                    pass
            raise AnkiConnectError(
                f"Cannot reach AnkiConnect at {self.url}. Is Anki running with the add-on?"
            ) from exc
        body = resp.json()
        resp.close()
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
        batches = (len(note_ids) + NOTES_INFO_BATCH - 1) // NOTES_INFO_BATCH
        logger.debug(f"AnkiConnect notesInfo ({batches} batch(es), {len(note_ids)} notes)")
        results: list[dict[str, Any]] = []
        for i in range(0, len(note_ids), NOTES_INFO_BATCH):
            batch = note_ids[i : i + NOTES_INFO_BATCH]
            results.extend(self.invoke("notesInfo", _log=False, notes=batch))
        return results

    def update_note_fields(self, note_id: int, fields: dict[str, str]) -> None:
        self.invoke("updateNoteFields", note={"id": note_id, "fields": fields})

    def add_tags(self, note_ids: list[int], tags: list[str]) -> None:
        if not note_ids or not tags:
            return
        self.invoke("addTags", notes=note_ids, tags=" ".join(tags))

    def remove_tags(self, note_ids: list[int], tags: list[str]) -> None:
        if not note_ids or not tags:
            return
        self.invoke("removeTags", notes=note_ids, tags=" ".join(tags))

    def store_media_file(self, filename: str, data: bytes) -> None:
        import base64

        self.invoke(
            "storeMediaFile",
            filename=filename,
            data=base64.b64encode(data).decode("ascii"),
        )


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


def preview_field_text(value: str, limit: int = 80) -> str:
    text = HTML_BREAK_RE.sub("\n", value)
    text = HTML_TAG_RE.sub(" ", text)
    text = WHITESPACE_RE.sub(" ", unescape(text)).strip()
    if not text:
        return "(empty)"
    return text if len(text) <= limit else text[: limit - 3] + "..."
