#!/usr/bin/env python3
"""Snapshot schema validation (fortress gate)."""

from __future__ import annotations

from typing import Any

REQUIRED_TOP = ("schemaVersion", "clubId", "games", "probedAt")
REQUIRED_GAME = ("id", "date", "competitionName")


def validate_snapshot(data: dict[str, Any], *, expected_club_id: int | None = None) -> list[str]:
    """Return a list of error strings; empty means OK."""
    errs: list[str] = []
    if not isinstance(data, dict):
        return ["snapshot is not an object"]
    for k in REQUIRED_TOP:
        if k not in data:
            errs.append(f"missing top-level field: {k}")
    if expected_club_id is not None and data.get("clubId") not in (expected_club_id, str(expected_club_id)):
        errs.append(f"clubId mismatch: got {data.get('clubId')!r}, expected {expected_club_id}")
    games = data.get("games")
    if not isinstance(games, list):
        errs.append("games must be a list")
        return errs
    if len(games) == 0:
        errs.append("games list is empty")
    seen: set[int] = set()
    for i, g in enumerate(games):
        if not isinstance(g, dict):
            errs.append(f"games[{i}] is not an object")
            continue
        for k in REQUIRED_GAME:
            if not g.get(k) and g.get(k) != 0:
                errs.append(f"games[{i}] missing {k}")
        try:
            gid = int(g["id"])
        except Exception:
            errs.append(f"games[{i}] id not int-like")
            continue
        if gid in seen:
            errs.append(f"duplicate game id {gid}")
        seen.add(gid)
        ds = str(g.get("date") or "")
        if len(ds) != 10 or ds[4] != "-" or ds[7] != "-":
            errs.append(f"games[{i}] bad date {ds!r}")
    return errs
