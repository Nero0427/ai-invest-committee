"""Configuration loading and persistence."""

from __future__ import annotations

import json
import os

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT, "config.json")

DEFAULTS = {
    "llm": {
        "provider": "deepseek",
        "base_url": "",
        "model": "",
        "api_key": "",
        "temperature": 0.7,
        "timeout": 90,
        "max_tokens": 1200,
    },
    "committee": {
        "debate_rounds": 2,
        "risk_rounds": 1,
        "language": "zh-CN",
    },
    "server": {
        "host": "127.0.0.1",
        "port": 8848,
        "open_browser": True,
    },
}


def _merge(base: dict, extra: dict) -> dict:
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value
    return base


def load_config(path: str | None = None) -> dict:
    """Defaults, overlaid with config.json when present."""
    cfg = json.loads(json.dumps(DEFAULTS))
    target = path or CONFIG_PATH
    if os.path.exists(target):
        try:
            with open(target, "r", encoding="utf-8") as handle:
                _merge(cfg, json.load(handle))
        except (OSError, json.JSONDecodeError):
            pass
    return cfg


def save_config(cfg: dict, path: str | None = None) -> None:
    target = path or CONFIG_PATH
    with open(target, "w", encoding="utf-8") as handle:
        json.dump(cfg, handle, ensure_ascii=False, indent=2)


def public_config(cfg: dict) -> dict:
    """Config safe to hand to the browser: never leak the key itself."""
    safe = json.loads(json.dumps(cfg))
    key = ((safe.get("llm") or {}).get("api_key") or "").strip()
    safe["llm"]["api_key"] = ""
    safe["llm"]["has_key"] = bool(key)
    return safe
