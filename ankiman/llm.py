from __future__ import annotations

import json
import os
import random
import re
import resource
import socket
import time
from typing import Any
from urllib.parse import urlparse

import requests
from loguru import logger

from .config import ModelConfig, ensure_api_key

JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```\s*$", re.DOTALL | re.IGNORECASE)
MAX_API_RETRIES = 3
RETRY_BASE = 2.0  # seconds base for exponential backoff

# Per-run DNS cache to avoid repeated lookups overwhelming the resolver.
_dns_cache: dict[tuple, list[tuple]] = {}
_orig_getaddrinfo = socket.getaddrinfo


def _cached_getaddrinfo(host: str, port: int, family: int = 0, type: int = 0, proto: int = 0, flags: int = 0) -> list[tuple]:
    key = (host, port, family, type, proto)
    if key not in _dns_cache:
        _dns_cache[key] = _orig_getaddrinfo(host, port, family, type, proto, flags)
    return _dns_cache[key]


def _retry_wait(attempt: int) -> float:
    """Return randomized wait seconds for retry attempt (0-indexed)."""
    base = RETRY_BASE * (2**attempt)
    return base * (0.5 + random.random())  # jitter: 0.5x to 1.5x of base


def _format_error(exc: Exception) -> str:
    """Extract a human-readable error from an API exception."""
    msg = _format_error_msg(exc)
    if "Too many open files" in msg or "EMFILE" in msg or "Errno 24" in msg:
        _log_fd_diag()
    return msg


def _log_fd_diag() -> None:
    """Log diagnostic info about file descriptors when EMFILE is hit."""
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        fd_count = len(os.listdir("/dev/fd"))
        logger.warning("FD diagnostic: open={} soft_limit={} hard_limit={}", fd_count, soft, hard)
    except Exception:
        pass


def _format_error_msg(exc: Exception) -> str:
    # requests.HTTPError has response with status + body
    if hasattr(exc, "response") and hasattr(exc.response, "status_code"):
        status = exc.response.status_code
        try:
            body = exc.response.text[:300]
        except Exception:
            body = "<no body>"
        return f"HTTP {status}: {body}"

    # requests.ConnectionError / network errors with cause chain
    if hasattr(exc, "request"):
        skip = {"Connection error.", ""}
        msgs: list[str] = []
        inner = exc
        while inner is not None:
            msg = str(inner).strip()
            if msg and msg not in skip:
                skip.add(msg)
                msgs.append(msg)
            inner = inner.__cause__ or inner.__context__
        detail = " | ".join(msgs[-3:])  # last 3 layers
        return f"Connection error: {detail}"
    # openai errors
    if hasattr(exc, "status_code"):
        body = getattr(exc, "body", None) or str(exc)
        return f"HTTP {exc.status_code}: {body}"
    return str(exc) or repr(exc)


def check_balance(
    model_cfg: ModelConfig,
    *,
    model_name: str | None = None,
    api_base: str | None = None,
) -> dict[str, str]:
    """Query the provider's balance endpoint. Returns a dict keyed by currency."""
    key = ensure_api_key(model_cfg.api_key_env, prompt=False)
    base = (api_base or model_cfg.api_base).rstrip("/")
    # Strip version suffix like /v1 to reach the root
    root = base.rsplit("/v", 1)[0]
    url = f"{root}/user/balance"
    resp = requests.get(url, headers={"Authorization": f"Bearer {key}"}, timeout=30)
    if resp.status_code == 404:
        raise RuntimeError("Balance endpoint not supported by this provider")
    resp.raise_for_status()
    data = resp.json()
    result: dict[str, str] = {}
    for info in data.get("balance_infos", []):
        currency = info.get("currency", "?")
        parts = []
        for label in ("total_balance", "granted_balance", "topped_up_balance"):
            val = info.get(label)
            if val:
                parts.append(f"{label}={val}")
        result[currency] = ", ".join(parts)
    if not result:
        raise RuntimeError(f"Unexpected balance response: {data}")
    return result


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
    missing = [field for field in target_fields if field not in data]
    if missing:
        raise ValueError(f"JSON missing required key(s): {', '.join(missing)}")
    return {field: str(data[field]) for field in target_fields}


class LLMClient:
    def __init__(self, model_cfg: ModelConfig, *, model_name: str | None, api_base: str | None) -> None:
        self.model = model_name or model_cfg.model
        self.api_base = (api_base or model_cfg.api_base).rstrip("/")
        self.api_key = ensure_api_key(model_cfg.api_key_env)
        socket.getaddrinfo = _cached_getaddrinfo  # activate DNS cache

    def complete(self, prompt: str) -> str:
        last_error: str = ""
        for attempt in range(MAX_API_RETRIES):
            try:
                if attempt:
                    wait = _retry_wait(attempt - 1)
                    logger.debug("Retry {}/{} after {:.1f}s", attempt + 1, MAX_API_RETRIES, wait)
                    time.sleep(wait)
                return self._call(prompt)
            except Exception as exc:
                last_error = _format_error(exc)
                logger.debug("API attempt {} failed: {}", attempt + 1, last_error)
        raise RuntimeError(f"API failed after {MAX_API_RETRIES} attempts: {last_error}")

    def _call(self, prompt: str) -> str:
        try:
            from openai import OpenAI  # noqa: PLC0415

            return self._call_openai(OpenAI, prompt)
        except ImportError:
            return self._call_requests(prompt)

    def _call_openai(self, OpenAI: type, prompt: str) -> str:
        client = OpenAI(api_key=self.api_key, base_url=self.api_base)
        response = client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            timeout=30,
        )
        content = response.choices[0].message.content
        if not content:
            raise RuntimeError("empty response from API")
        return content

    def _call_requests(self, prompt: str) -> str:
        url = f"{self.api_base}/chat/completions"
        resp = None
        try:
            resp = requests.post(
                url,
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json={"model": self.model, "messages": [{"role": "user", "content": prompt}]},
                timeout=30,
            )
            resp.raise_for_status()
            body = resp.json()
            return body["choices"][0]["message"]["content"]
        except requests.RequestException:
            raise
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"unexpected API response shape: {body}") from exc
        finally:
            if resp is not None:
                resp.close()
