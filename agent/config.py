"""Configuration loading (TOML) with CLI overrides like --set load.users=50."""
from __future__ import annotations

import copy
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent


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


def load_config(path: str | Path | None = None, overrides: list[str] | None = None) -> dict:
    path = Path(path) if path else ROOT / "config.toml"
    with open(path, "rb") as fh:
        cfg = tomllib.load(fh)
    cfg = copy.deepcopy(cfg)
    for item in overrides or []:
        key, _, raw = item.partition("=")
        section, _, name = key.partition(".")
        if not name or section not in cfg:
            raise SystemExit(f"bad --set '{item}' (use section.key=value, sections: {', '.join(cfg)})")
        cfg[section][name] = _coerce(raw)
    return cfg
