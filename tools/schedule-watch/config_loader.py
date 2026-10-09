#!/usr/bin/env python3
"""Load schedule-watch YAML config (active club profile)."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config.yaml"

DEFAULTS: dict[str, Any] = {
    "schemaVersion": 1,
    "club": {
        "id": 78,
        "name": "Floorballbunnies",
        "slug": "floorballbunnies",
        "url": "https://www.floorballbunnies.at",
        "logoUrl": "",
        "nameMatchers": ["Floorballbunnies", "Floorballbunnies Wien"],
    },
    "mcp": {"host": "mcp.floorballflash.at", "timeoutSeconds": 180, "maxAttempts": 20},
    "schedule": {
        "cron": "0 7 * * 1",
        "timezone": "Europe/Vienna",
        "watchFields": [
            "date",
            "time",
            "venue",
            "state",
            "homeTeam",
            "awayTeam",
            "competitionId",
            "competitionName",
        ],
    },
    "fortress": {
        "minGameRatio": 0.70,
        "maxAddedGames": 100,
        "maxRemovedGames": 100,
        "requireHttpsPagesUrl": True,
        "validateSnapshot": True,
        "csp": True,
    },
    "brand": {"lime": "#ccff00", "ink": "#0d0d0d"},
    "coaches": {},
    "coachesNotifyDefaultFull": False,
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def resolve_config_path(explicit: str | Path | None = None) -> Path:
    if explicit:
        return Path(explicit)
    env = os.getenv("SCHEDULE_WATCH_CONFIG", "").strip()
    if env:
        return Path(env)
    return DEFAULT_CONFIG


@lru_cache(maxsize=4)
def load_config(path_str: str | None = None) -> dict[str, Any]:
    path = resolve_config_path(path_str)
    data: dict[str, Any] = {}
    if path.exists():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"Config root must be a mapping: {path}")
        data = loaded
    cfg = _deep_merge(DEFAULTS, data)

    # Env overrides for multi-club / CI
    if os.getenv("CLUB_ID"):
        cfg["club"]["id"] = int(os.environ["CLUB_ID"])
    if os.getenv("CLUB_NAME"):
        cfg["club"]["name"] = os.environ["CLUB_NAME"]
    if os.getenv("MCP_HOST"):
        cfg["mcp"]["host"] = os.environ["MCP_HOST"]

    club_id = int(cfg["club"]["id"])
    if club_id <= 0:
        raise ValueError("club.id must be a positive FloorballFlash clubId")
    return cfg


def clear_config_cache() -> None:
    load_config.cache_clear()
