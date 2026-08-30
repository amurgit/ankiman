from __future__ import annotations

from html import unescape
from typing import Any, Callable

from jinja2 import Environment, TemplateSyntaxError, meta

from .anki import HTML_BREAK_RE, HTML_TAG_RE, WHITESPACE_RE
from .util import parse_comma_list

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


def extract_filter_fields(expression: str) -> list[str]:
    env = _make_filter_env()
    try:
        ast = env.parse("{% if " + expression.strip() + " %}{% endif %}")
    except TemplateSyntaxError as exc:
        raise SystemExit(f"Invalid filter expression: {exc}") from exc
    return sorted(meta.find_undeclared_variables(ast), key=str.lower)


def resolve_field_names(names: list[str], available: list[str]) -> list[str]:
    lower_to_actual = {name.lower(): name for name in available}
    resolved: list[str] = []
    unknown: list[str] = []
    for name in names:
        key = name.lstrip("+").strip()
        if not key:
            continue
        actual = lower_to_actual.get(key.lower())
        if actual:
            if actual not in resolved:
                resolved.append(actual)
        else:
            unknown.append(key)
    if unknown:
        raise SystemExit(
            f"Unknown field(s): {', '.join(unknown)}. "
            f"Available: {', '.join(sorted(available))}"
        )
    return resolved


def resolve_show_fields(
    fields_opt: str | None,
    filter_expr: str | None,
    available: list[str],
) -> list[str]:
    """Pick which note fields to display in show."""
    if fields_opt:
        parts = parse_comma_list(fields_opt)
        if any(part.startswith("+") for part in parts):
            base = resolve_show_fields(None, filter_expr, available)
            return resolve_field_names(base + [p.lstrip("+") for p in parts], available)
        return resolve_field_names(parts, available)
    if filter_expr:
        return resolve_field_names(extract_filter_fields(filter_expr), available)
    return sorted(available)


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
