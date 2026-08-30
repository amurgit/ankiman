from __future__ import annotations

import asyncio

# Canonical language id -> default edge-tts voice
DEFAULT_VOICES: dict[str, str] = {
    "yue-hk": "zh-HK-HiuGaaiNeural",
    "zh-cn": "zh-CN-XiaoxiaoNeural",
    "en-us": "en-US-JennyNeural",
}

# User-facing aliases -> canonical id
LANGUAGE_ALIASES: dict[str, str] = {
    "cantonese": "yue-hk",
    "yue": "yue-hk",
    "yue-hk": "yue-hk",
    "zh-hk": "yue-hk",
    "mandarin": "zh-cn",
    "putonghua": "zh-cn",
    "zh-cn": "zh-cn",
    "english": "en-us",
    "en": "en-us",
    "en-us": "en-us",
}


class TTSError(RuntimeError):
    pass


def language_choices() -> list[str]:
    return sorted(LANGUAGE_ALIASES.keys())


def resolve_language(language: str) -> str:
    key = language.strip().lower()
    canonical = LANGUAGE_ALIASES.get(key)
    if not canonical:
        raise SystemExit(
            f"Unknown language {language!r}. "
            f"Choose one of: {', '.join(language_choices())}"
        )
    return canonical


def resolve_voice(*, language: str, voice: str | None = None) -> tuple[str, str]:
    canonical = resolve_language(language)
    resolved = (voice or "").strip() or DEFAULT_VOICES[canonical]
    return canonical, resolved


async def _synthesize_async(text: str, voice: str) -> bytes:
    import edge_tts

    communicate = edge_tts.Communicate(text, voice)
    chunks: list[bytes] = []
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            chunks.append(chunk["data"])
    if not chunks:
        raise TTSError("No audio returned")
    return b"".join(chunks)


def synthesize(text: str, *, voice: str) -> bytes:
    text = text.strip()
    if not text:
        raise TTSError("Text is empty")
    try:
        return asyncio.run(_synthesize_async(text, voice))
    except TTSError:
        raise
    except Exception as exc:
        raise TTSError(str(exc)) from exc
