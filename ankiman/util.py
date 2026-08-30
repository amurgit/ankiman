from __future__ import annotations

import re
from dataclasses import dataclass

PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")


def parse_comma_list(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def build_anki_query(deck: str | None, tags: list[str] | None) -> str:
    parts: list[str] = []
    if deck:
        parts.append(f'deck:"{deck}"')
    if tags:
        tag_clauses = " OR ".join(f'tag:"{t}"' for t in tags)
        parts.append(f"({tag_clauses})")
    return " ".join(parts)


def replace_placeholders(text: str, field_map: dict[str, str]) -> str:
    def repl(match: re.Match[str]) -> str:
        return field_map.get(match.group(1), "")

    return PLACEHOLDER_RE.sub(repl, text)


def extract_source_fields(prompt: str) -> list[str]:
    seen: set[str] = set()
    fields: list[str] = []
    for match in PLACEHOLDER_RE.finditer(prompt):
        name = match.group(1)
        if name not in seen:
            seen.add(name)
            fields.append(name)
    return fields


@dataclass
class FillStats:
    total: int = 0
    processed: int = 0
    skipped: int = 0
    errors: int = 0


def play_audio_file(path) -> None:
    import shutil
    import subprocess
    import sys
    from pathlib import Path

    path = Path(path).resolve()
    if sys.platform == "darwin":
        subprocess.run(["afplay", str(path)], check=True)
        return
    for cmd in (["ffplay", "-nodisp", "-autoexit", str(path)], ["mpv", "--no-video", str(path)]):
        if shutil.which(cmd[0]):
            subprocess.run(cmd, check=True)
            return
    raise SystemExit(f"No audio player found. Open manually: {path}")
