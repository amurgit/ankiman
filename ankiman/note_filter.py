from __future__ import annotations

from html import unescape
from typing import Any, Callable

from jinja2 import Environment, TemplateSyntaxError

from .anki import HTML_BREAK_RE, HTML_TAG_RE, WHITESPACE_RE

FilterExpr = Callable[..., bool]


def plain_field_text(value: str) -> str:
    text = HTML_BREAK_RE.sub("\n", value)
    text = HTML_TAG_RE.sub(" ", text)
    return WHITESPACE_RE.sub(" ", unescape(text)).strip()


def split_word_in(haystack: str, word: str) -> bool:
    """True when every character of word appears in order inside haystack (gaps allowed)."""
    word = plain_field_text(word)
    haystack = plain_field_text(haystack)
    if not word:
        return True
    if len(word) == 1:
        return word in haystack
    index = 0
    for char in haystack:
        if char == word[index]:
            index += 1
            if index == len(word):
                return True
    return False


def field_context(fields: dict[str, str]) -> dict[str, str]:
    ctx: dict[str, str] = {}
    for name, raw in fields.items():
        text = plain_field_text(raw)
        ctx[name.lower()] = text
        if name.isidentifier():
            ctx[name] = text
    return ctx


def _make_filter_env() -> Environment:
    env = Environment()
    env.tests["empty"] = lambda value: not plain_field_text(str(value))
    env.tests["split_word_in"] = lambda word, text: split_word_in(str(text), str(word))
    return env


def compile_note_filter(expression: str) -> FilterExpr:
    env = _make_filter_env()
    try:
        return env.compile_expression(expression.strip())
    except TemplateSyntaxError as exc:
        raise SystemExit(f"Invalid filter expression: {exc}") from exc


def note_matches(compiled: FilterExpr, fields: dict[str, str]) -> bool:
    ctx = field_context(fields)
    try:
        return bool(compiled(**ctx))
    except Exception as exc:
        raise SystemExit(f"Filter evaluation failed: {exc}") from exc


def filter_notes(notes: list[dict[str, Any]], expression: str) -> list[dict[str, Any]]:
    compiled = compile_note_filter(expression)
    matched: list[dict[str, Any]] = []
    for note in notes:
        fields = {
            name: (info.get("value") or "")
            for name, info in (note.get("fields") or {}).items()
        }
        if note_matches(compiled, fields):
            matched.append(note)
    return matched
