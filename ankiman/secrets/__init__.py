from __future__ import annotations

import os
import sys
from getpass import getpass
from pathlib import Path

import structlog
from dotenv import load_dotenv

from .base import SecretStore
from .env import EnvFileStore

logger = structlog.get_logger()


def get_store(path: Path | None = None) -> SecretStore:
    if sys.platform == "darwin":
        from .macos import MacOSKeychainStore

        return MacOSKeychainStore()
    return EnvFileStore(path)


def secret_backend_name() -> str:
    if sys.platform == "darwin":
        return "macOS Keychain"
    return ".env file"


def get_secret(key: str, *, path: Path | None = None) -> str | None:
    value = get_store(path).get(key)
    if value:
        return value
    load_dotenv((path or Path.cwd()) / ".env")
    env_value = os.environ.get(key, "").strip()
    return env_value or None


def set_secret(key: str, value: str, *, path: Path | None = None) -> None:
    get_store(path).set(key, value)


def delete_secret(key: str, *, path: Path | None = None) -> bool:
    return get_store(path).delete(key)


def ensure_secret(key: str, *, prompt: bool = True, path: Path | None = None) -> str:
    value = get_secret(key, path=path)
    if value:
        return value
    if not prompt:
        raise SystemExit(f"Secret {key!r} is not set.")
    backend = secret_backend_name()
    logger.info(f"{key} not found — enter it now (saved to {backend})")
    value = getpass(f"{key}: ").strip()
    if not value:
        raise SystemExit(f"Empty API key for {key}.")
    set_secret(key, value, path=path)
    return value
