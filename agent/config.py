"""Configuration loading (TOML): built-in defaults <- your config file <- CLI overrides like --set load.users=50."""
from __future__ import annotations

import copy
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULTS = Path(__file__).resolve().parent / "defaults.toml"


def _coerce(raw: str) -> Any:
    low = raw.lower()
    if low in ("true", "false"):
        return low == "true"
    for cast in (int, float):
        try:
            return cast(raw)
        except ValueError:
            pass
    return raw


def _merge(base: dict, over: dict) -> dict:
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def resolve(path: str | Path, base: Path | None = None) -> Path:
    """A config path is relative to this repo unless it is absolute."""
    p = Path(path)
    return p if p.is_absolute() else (base or ROOT) / p


def load_config(path: str | Path | None = None, overrides: list[str] | None = None) -> dict:
    with open(DEFAULTS, "rb") as fh:
        cfg = tomllib.load(fh)
    path = Path(path) if path else ROOT / "config.toml"
    if not path.exists():
        raise SystemExit(f"config file not found: {path}")
    with open(path, "rb") as fh:
        _merge(cfg, tomllib.load(fh))
    cfg = copy.deepcopy(cfg)
    for item in overrides or []:
        key, _, raw = item.partition("=")
        section, _, name = key.partition(".")
        if not name or section not in cfg:
            raise SystemExit(f"bad --set '{item}' (use section.key=value, sections: {', '.join(cfg)})")
        cfg[section][name] = _coerce(raw)
    return cfg
