from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
import structlog

from .secrets import ensure_secret

CONFIG_FILENAME = ".ankiman_config.yaml"
ENV_FILENAME = ".env"

logger = structlog.get_logger()


def default_env_var(model_name: str) -> str:
    return f"{model_name}_API_KEY".upper()


@dataclass
class ModelConfig:
    name: str
    model: str
    api_base: str
    api_key_env: str = ""

    def __post_init__(self) -> None:
        if not self.api_key_env:
            self.api_key_env = default_env_var(self.name)


@dataclass
class AppConfig:
    default_model: str
    models: dict[str, ModelConfig]

    def resolve(self, name: str | None) -> ModelConfig:
        key = name or self.default_model
        if key not in self.models:
            available = ", ".join(sorted(self.models)) or "(none)"
            raise SystemExit(
                f"Unknown model {key!r}. Available: {available}. "
                f"Add one with: ankiman model add <name> ..."
            )
        return self.models[key]


def config_path(cwd: Path | None = None) -> Path:
    return (cwd or Path.cwd()) / CONFIG_FILENAME


def load_config(path: Path | None = None) -> AppConfig:
    path = path or config_path()
    if not path.is_file():
        raise SystemExit(
            f"Config not found: {path}\n"
            f"Create one with: ankiman model add"
        )
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    default = raw.get("default")
    models_raw = raw.get("models") or {}
    if not default:
        raise SystemExit(f"Missing 'default' in {path}")
    if not models_raw:
        raise SystemExit(f"Missing 'models' in {path}")
    models: dict[str, ModelConfig] = {}
    for name, entry in models_raw.items():
        if not isinstance(entry, dict):
            raise SystemExit(f"Invalid model entry {name!r} in {path}")
        for field in ("model", "api_base"):
            if field not in entry:
                raise SystemExit(f"Model {name!r} missing required field {field!r}")
        models[name] = ModelConfig(
            name=name,
            model=str(entry["model"]),
            api_base=str(entry["api_base"]).rstrip("/"),
            api_key_env=str(entry.get("api_key_env", "ANKI_LLM_API_KEY")),
        )
    return AppConfig(default_model=str(default), models=models)


def save_config(app: AppConfig, path: Path | None = None) -> None:
    path = path or config_path()
    data: dict[str, Any] = {
        "default": app.default_model,
        "models": {
            name: {
                "model": mc.model,
                "api_base": mc.api_base,
                "api_key_env": mc.api_key_env,
            }
            for name, mc in app.models.items()
        },
    }
    path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False), encoding="utf-8")


def ensure_api_key(env_var: str, *, prompt: bool = True) -> str:
    return ensure_secret(env_var, prompt=prompt, path=env_path())


def env_path(cwd: Path | None = None) -> Path:
    return (cwd or Path.cwd()) / ENV_FILENAME


def reset_config(*, delete_keys: bool = False) -> list[str]:
    """Remove .ankiman_config.yaml. Returns api_key refs if delete_keys is True."""
    path = config_path()
    key_refs: list[str] = []
    if not path.is_file():
        return key_refs
    if delete_keys:
        app_cfg = load_config(path)
        key_refs = sorted({mc.api_key_env for mc in app_cfg.models.values()})
    path.unlink()
    return key_refs
