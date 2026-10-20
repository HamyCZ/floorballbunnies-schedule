#!/usr/bin/env python3
"""Skip a scheduled watch if this ISO week already had a successful run (Vienna).

Used by GitHub Actions as a same-week fallback guard. Manual workflow_dispatch
runs should not call this (or should ignore skip_run).

Reads last-success.json (savedAt / probedAt) and compares ISO week + year in
Europe/Vienna against "now". Optionally also treats success within the last
~20 hours as same-window (covers delayed Monday backups).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Delayed primary + backup can be many hours apart; ~20h still same Monday window.
RECENT_HOURS = 20


def _vienna_tz():
    """Europe/Vienna; tzdata package or OS zoneinfo. Fallback UTC+1 if missing."""
    try:
        return ZoneInfo("Europe/Vienna")
    except ZoneInfoNotFoundError:
        return timezone(timedelta(hours=1))


def parse_utc(value: str | None) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def success_timestamp(payload: dict) -> datetime | None:
    return parse_utc(payload.get("savedAt")) or parse_utc(payload.get("probedAt"))


def same_vienna_iso_week(a: datetime, b: datetime) -> bool:
    tz = _vienna_tz()
    va = a.astimezone(tz)
    vb = b.astimezone(tz)
    return va.isocalendar()[:2] == vb.isocalendar()[:2]


def should_skip(last_success: Path, *, now: datetime | None = None) -> tuple[bool, str]:
    now = now or datetime.now(timezone.utc)
    if not last_success.is_file():
        return False, f"No last-success file at {last_success}"

    try:
        payload = json.loads(last_success.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return False, f"Could not read last-success: {e}"

    if not isinstance(payload, dict):
        return False, "last-success is not an object"

    when = success_timestamp(payload)
    if when is None:
        return False, "last-success missing savedAt/probedAt"

    if same_vienna_iso_week(when, now):
        return (
            True,
            f"Already succeeded this week — skipping fallback run "
            f"(last success {when.isoformat()}, Vienna ISO week matches)",
        )

    age = now - when
    if timedelta(0) <= age <= timedelta(hours=RECENT_HOURS):
        return (
            True,
            f"Already succeeded this week — skipping fallback run "
            f"(last success {when.isoformat()}, within {RECENT_HOURS}h)",
        )

    return (
        False,
        f"Last success {when.isoformat()} is outside this Vienna week / {RECENT_HOURS}h window",
    )


def write_gh_output(**kwargs: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        for key, value in kwargs.items():
            fh.write(f"{key}={value}\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--last-success",
        type=Path,
        default=Path("data/last-success.json"),
        help="Path to last-success.json (prefer tip of main in Actions)",
    )
    args = ap.parse_args()

    skip, reason = should_skip(args.last_success)
    print(reason)
    write_gh_output(skip_run="yes" if skip else "no", skip_reason=reason)
    return 0


if __name__ == "__main__":
    sys.exit(main())
