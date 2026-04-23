"""Configuration loader — parses config.yaml and provides AttrDict access with defaults"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml


class AttrDict(dict):
    """A dict that supports obj.key attribute-style access"""

    def __getattr__(self, key: str) -> Any:
        try:
            val = self[key]
            if isinstance(val, dict):
                return AttrDict(val)
            return val
        except KeyError:
            raise AttributeError(f"Config has no key: {key!r}")

    def get(self, key: str, default: Any = None) -> Any:
        val = super().get(key, default)
        if isinstance(val, dict):
            return AttrDict(val)
        return val


def load_config(path: str | Path | None = None) -> AttrDict:
    """Load the configuration file. When no path is specified, searches in order:
    1. Environment variable LARKSH_CONFIG
    2. config.yaml in the current directory
    3. config.yaml in the same directory as this script
    """
    candidates = []
    if path:
        candidates.append(Path(path))
    env_path = os.getenv("LARKSH_CONFIG")
    if env_path:
        candidates.append(Path(env_path))
    candidates.append(Path.cwd() / "config.yaml")
    candidates.append(Path(__file__).parent.parent / "config.yaml")

    for p in candidates:
        if p.exists():
            with open(p, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            # Apply environment variable overrides for key fields
            _apply_env_overrides(data)
            return AttrDict(data)

    raise FileNotFoundError(
        f"Config file not found. Tried: {[str(c) for c in candidates]}\n"
        "Set LARKSH_CONFIG env var or place config.yaml in working directory."
    )


def _apply_env_overrides(data: dict) -> None:
    """Allow sensitive config fields such as FEISHU_APP_ID and FEISHU_APP_SECRET to be overridden via environment variables"""
    mapping = {
        "FEISHU_APP_ID": ("feishu", "app_id"),
        "FEISHU_APP_SECRET": ("feishu", "app_secret"),
        "FEISHU_WEBHOOK_TOKEN": ("feishu", "webhook", "verification_token"),
        "FEISHU_ENCRYPT_KEY": ("feishu", "webhook", "encrypt_key"),
    }
    for env_key, path in mapping.items():
        val = os.getenv(env_key)
        if val:
            node = data
            for segment in path[:-1]:
                node = node.setdefault(segment, {})
            node[path[-1]] = val
