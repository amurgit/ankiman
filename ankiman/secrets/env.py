from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv, set_key, unset_key

ENV_FILENAME = ".env"


class EnvFileStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or Path.cwd() / ENV_FILENAME

    def get(self, key: str) -> str | None:
        load_dotenv(self.path)
        value = os.environ.get(key, "").strip()
        return value or None

    def set(self, key: str, value: str) -> None:
        if not self.path.is_file():
            self.path.write_text("", encoding="utf-8")
        set_key(str(self.path), key, value)
        load_dotenv(self.path, override=True)

    def delete(self, key: str) -> bool:
        if not self.path.is_file():
            return False
        load_dotenv(self.path)
        if not os.environ.get(key):
            return False
        unset_key(str(self.path), key)
        if self.path.read_text(encoding="utf-8").strip() == "":
            self.path.unlink(missing_ok=True)
        return True
