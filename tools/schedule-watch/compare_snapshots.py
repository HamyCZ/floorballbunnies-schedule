#!/usr/bin/env python3
"""Compare two schedule snapshots and report field-level changes."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from config_loader import load_config

DEFAULT_WATCH_FIELDS = (
    "date",
    "time",
    "venue",
    "state",
    "homeTeam",
    "awayTeam",
    "competitionId",
    "competitionName",
)

# States that mean the fixture is no longer a normal upcoming game
CANCEL_HINTS = ("cancel", "abandon", "postpon", "walkover", "forfeit")


def load_snapshot(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if "games" not in data:
        raise ValueError(f"{path} missing 'games'")
    return data


def index_games(snapshot: dict[str, Any]) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for g in snapshot.get("games") or []:
        gid = int(g["id"])
        out[gid] = g
    return out


def norm(v: Any) -> str:
    if v is None:
        return ""
    return str(v).strip()


def is_cancel_state(state: Any) -> bool:
    s = norm(state).lower()
    return any(h in s for h in CANCEL_HINTS)


def opponent_of(game: dict[str, Any]) -> str:
    if game.get("opponent"):
        return norm(game["opponent"])
    ha = norm(game.get("homeAway")).lower()
    home, away = norm(game.get("homeTeam")), norm(game.get("awayTeam"))
    if ha == "home":
        return away
    if ha == "away":
        return home
    return f"{home} vs {away}".strip()


def resolve_watch_fields(
    old: dict[str, Any],
    new: dict[str, Any],
    cfg: dict[str, Any] | None = None,
) -> tuple[str, ...]:
    """Prefer snapshot-embedded watchFields, then config, then defaults."""
    for source in (new.get("watchFields"), old.get("watchFields")):
        if isinstance(source, list) and source:
            return tuple(str(f) for f in source)
    if cfg is None:
        cfg = load_config()
    fields = cfg.get("schedule", {}).get("watchFields") or DEFAULT_WATCH_FIELDS
    return tuple(str(f) for f in fields)


def diff_snapshots(
    old: dict[str, Any],
    new: dict[str, Any],
    watch_fields: Sequence[str] | None = None,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = cfg or load_config()
    fields = tuple(watch_fields) if watch_fields else resolve_watch_fields(old, new, cfg)
    club = cfg.get("club") or {}
    club_name = club.get("name") or new.get("clubName") or old.get("clubName") or "Club"
    club_id = new.get("clubId") or old.get("clubId") or club.get("id")

    old_idx = index_games(old)
    new_idx = index_games(new)
    old_ids = set(old_idx)
    new_ids = set(new_idx)

    added = []
    removed = []
    changed = []
    cancellations = []

    for gid in sorted(new_ids - old_ids):
        g = new_idx[gid]
        added.append(
            {
                "id": gid,
                "game": g,
                "summary": f"New game #{gid}: {g.get('date')} {g.get('time') or 'TBD'} · {g.get('competitionName')} · vs {opponent_of(g)} · {g.get('venue')}",
            }
        )

    for gid in sorted(old_ids - new_ids):
        g = old_idx[gid]
        removed.append(
            {
                "id": gid,
                "game": g,
                "summary": f"Removed game #{gid}: was {g.get('date')} {g.get('time') or 'TBD'} · {g.get('competitionName')} · vs {opponent_of(g)} · {g.get('venue')}",
            }
        )

    for gid in sorted(old_ids & new_ids):
        a, b = old_idx[gid], new_idx[gid]
        field_changes = []
        for field in fields:
            av, bv = norm(a.get(field)), norm(b.get(field))
            if av != bv:
                field_changes.append({"field": field, "from": a.get(field), "to": b.get(field)})
        if not field_changes:
            continue

        entry = {
            "id": gid,
            "before": a,
            "after": b,
            "changes": field_changes,
            "summary": _change_summary(gid, a, b, field_changes),
        }
        changed.append(entry)

        state_change = next((c for c in field_changes if c["field"] == "state"), None)
        if state_change and is_cancel_state(state_change["to"]) and not is_cancel_state(state_change["from"]):
            cancellations.append(entry)
        elif state_change and norm(state_change["to"]) and norm(state_change["to"]) != norm(state_change["from"]):
            if "cancel" in norm(state_change["to"]).lower() or "postpon" in norm(state_change["to"]).lower():
                cancellations.append(entry)

    return {
        "comparedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "oldProbedAt": old.get("probedAt"),
        "newProbedAt": new.get("probedAt"),
        "clubId": club_id,
        "clubName": club_name,
        "watchFields": list(fields),
        "counts": {
            "added": len(added),
            "removed": len(removed),
            "changed": len(changed),
            "cancellations": len(cancellations),
            "totalEvents": len(added) + len(removed) + len(changed),
        },
        "added": added,
        "removed": removed,
        "changed": changed,
        "cancellations": cancellations,
        "hasChanges": bool(added or removed or changed),
    }


def _change_summary(gid: int, before: dict, after: dict, changes: list[dict]) -> str:
    bits = []
    for c in changes:
        bits.append(f"{c['field']}: {c['from']!r} → {c['to']!r}")
    league = after.get("competitionName") or before.get("competitionName")
    return f"#{gid} ({league}): " + "; ".join(bits)


def render_markdown(report: dict[str, Any]) -> str:
    club_name = report.get("clubName") or "Club"
    fields = report.get("watchFields") or list(DEFAULT_WATCH_FIELDS)
    lines = [
        f"# {club_name} — schedule changes",
        "",
        f"_Compared at `{report['comparedAt']}` · previous snapshot `{report.get('oldProbedAt')}` · new `{report.get('newProbedAt')}`_",
        "",
        "## Summary",
        "",
        f"| Added | Removed | Changed | Cancellations flagged |",
        f"|---:|---:|---:|---:|",
        f"| {report['counts']['added']} | {report['counts']['removed']} | {report['counts']['changed']} | {report['counts']['cancellations']} |",
        "",
    ]
    if not report["hasChanges"]:
        lines += ["_No schedule changes detected._", ""]
        return "\n".join(lines)

    if report["cancellations"]:
        lines += ["## Cancellations / state alerts", ""]
        for e in report["cancellations"]:
            lines.append(f"- {e['summary']}")
        lines.append("")

    if report["changed"]:
        lines += ["## Field changes (time / venue / teams / date / state / competition)", ""]
        for e in report["changed"]:
            lines.append(f"### Game #{e['id']} — {e['after'].get('competitionName')}")
            lines.append("")
            lines.append(f"- Opponent: **{opponent_of(e['after'])}**")
            for c in e["changes"]:
                lines.append(f"- **{c['field']}**: `{c['from']}` → `{c['to']}`")
            lines.append("")

    if report["added"]:
        lines += ["## New games", ""]
        for e in report["added"]:
            lines.append(f"- {e['summary']}")
        lines.append("")

    if report["removed"]:
        lines += ["## Removed games", ""]
        for e in report["removed"]:
            lines.append(f"- {e['summary']}")
        lines.append("")

    lines += [
        "## Watched fields",
        "",
        ", ".join(f"`{f}`" for f in fields),
        "",
        "Score-only updates are ignored (not in watch list).",
        "",
    ]
    return "\n".join(lines)


def render_html(report: dict[str, Any], pages_base_url: str = "") -> str:
    """Email-safe digest (tables + inline CSS). Not the GitHub Pages UI."""

    def esc(s: Any) -> str:
        return (
            str(s if s is not None else "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )

    base = (pages_base_url or "").strip().rstrip("/")
    # Reject non-http(s) / injectable base URLs
    low = base.lower()
    if base and not (low.startswith("https://") or low.startswith("http://")):
        base = ""
    if any(c in base for c in (" ", "\n", "\r", "`", "<", ">")):
        base = ""
    links = ""
    if base:
        links = f"""
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:16px 0 8px">
  <tr><td style="background:#ccff00;color:#000;font:700 13px Arial,sans-serif;padding:12px 14px">
    Open the schedule site (full weekend + clashes):
  </td></tr>
  <tr><td style="padding:10px 0 0;font:14px/1.45 Arial,sans-serif;color:#111">
    <a href="{esc(base)}/" style="color:#0d0d0d;font-weight:700">Home</a>
    &nbsp;·&nbsp;
    <a href="{esc(base)}/weekend.html" style="color:#0d0d0d;font-weight:700">Next weekend</a>
    &nbsp;·&nbsp;
    <a href="{esc(base)}/clashes.html" style="color:#0d0d0d;font-weight:700">Clashes</a>
    &nbsp;·&nbsp;
    <a href="{esc(base)}/calendar.html" style="color:#0d0d0d;font-weight:700">Calendar</a>
    &nbsp;·&nbsp;
    <a href="{esc(base)}/changes.html" style="color:#0d0d0d;font-weight:700">Changes</a>
  </td></tr>
</table>
"""

    if not report["hasChanges"]:
        body = "<p style='margin:0;color:#444'><em>No schedule changes detected.</em></p>"
    else:
        parts = [
            f"<p style='margin:0 0 12px;font:15px/1.45 Arial,sans-serif'>"
            f"<strong>{report['counts']['changed']}</strong> changed · "
            f"<strong>{report['counts']['added']}</strong> added · "
            f"<strong>{report['counts']['removed']}</strong> removed"
            f"{' · <strong style=\"color:#b00020\">' + str(report['counts']['cancellations']) + ' cancellation(s)</strong>' if report['counts']['cancellations'] else ''}"
            f"</p>"
        ]
        if report["cancellations"]:
            parts.append("<p style='margin:0 0 8px;font:700 14px Arial,sans-serif;color:#b00020'>Cancellations / state alerts</p><ul style='margin:0 0 14px;padding-left:18px'>")
            for e in report["cancellations"]:
                parts.append(f"<li style='margin:0 0 6px'>{esc(e['summary'])}</li>")
            parts.append("</ul>")
        if report["changed"]:
            parts.append("<p style='margin:0 0 8px;font:700 14px Arial,sans-serif'>Field changes</p>")
            for e in report["changed"]:
                parts.append(
                    "<table role='presentation' width='100%' cellpadding='0' cellspacing='0' "
                    "style='margin:0 0 10px;border-left:4px solid #ccff00;background:#f7f7f7'>"
                    f"<tr><td style='padding:10px 12px;font:14px/1.45 Arial,sans-serif'>"
                    f"<strong>#{e['id']}</strong> · {esc(e['after'].get('competitionName'))}<br>"
                    f"<span style='color:#555'>vs {esc(opponent_of(e['after']))}</span><br>"
                )
                for c in e["changes"]:
                    parts.append(
                        f"· <strong>{esc(c['field'])}</strong>: "
                        f"<code>{esc(c['from'])}</code> → <code>{esc(c['to'])}</code><br>"
                    )
                parts.append("</td></tr></table>")
        if report["added"]:
            parts.append("<p style='margin:12px 0 8px;font:700 14px Arial,sans-serif'>New games</p><ul style='margin:0 0 14px;padding-left:18px'>")
            for e in report["added"]:
                parts.append(f"<li style='margin:0 0 6px'>{esc(e['summary'])}</li>")
            parts.append("</ul>")
        if report["removed"]:
            parts.append("<p style='margin:12px 0 8px;font:700 14px Arial,sans-serif'>Removed games</p><ul style='margin:0 0 14px;padding-left:18px'>")
            for e in report["removed"]:
                parts.append(f"<li style='margin:0 0 6px'>{esc(e['summary'])}</li>")
            parts.append("</ul>")
        body = "\n".join(parts)

    club_name = report.get("clubName") or "Club"
    lime = "#ccff00"
    ink = "#0d0d0d"
    try:
        brand = load_config().get("brand") or {}
        lime = brand.get("lime") or lime
        ink = brand.get("ink") or ink
    except Exception:
        pass

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(club_name)} schedule changes</title></head>
<body style="margin:0;padding:0;background:#ececec">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#ececec;padding:16px 0">
<tr><td align="center">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#ffffff;border-collapse:collapse">
  <tr><td style="background:{esc(ink)};padding:16px 18px">
    <div style="font:700 11px/1 Arial,sans-serif;letter-spacing:.14em;text-transform:uppercase;color:{esc(lime)}">{esc(club_name)}</div>
    <div style="font:700 20px/1.2 Arial,sans-serif;color:#ffffff;margin-top:6px">Schedule changes</div>
  </td></tr>
  <tr><td style="padding:16px 18px;font:13px/1.4 Arial,sans-serif;color:#666">
    Compared {esc(report['comparedAt'])}<br>
    Previous snapshot {esc(report.get('oldProbedAt'))} · new {esc(report.get('newProbedAt'))}
  </td></tr>
  <tr><td style="padding:0 18px 18px">{body}{links}
    <p style="margin:18px 0 0;font:12px/1.4 Arial,sans-serif;color:#888">
      This email is a short digest only. The full weekend list and clash view live on the website — not in this message.
    </p>
  </td></tr>
</table>
</td></tr></table>
</body></html>
"""


def main() -> int:
    ap = argparse.ArgumentParser(description="Diff two schedule snapshots")
    ap.add_argument("--old", type=Path, required=True, help="Previous snapshot JSON")
    ap.add_argument("--new", type=Path, required=True, help="New snapshot JSON")
    ap.add_argument("--out-json", type=Path, help="Write machine-readable diff JSON")
    ap.add_argument("--out-md", type=Path, help="Write Markdown report")
    ap.add_argument("--out-html", type=Path, help="Write email-safe HTML digest")
    ap.add_argument("--pages-base-url", default="", help="Public Pages URL linked from the email digest")
    ap.add_argument("--config", type=Path, default=None)
    args = ap.parse_args()

    cfg = load_config(str(args.config) if args.config else None)
    report = diff_snapshots(load_snapshot(args.old), load_snapshot(args.new), cfg=cfg)

    if args.out_json:
        args.out_json.parent.mkdir(parents=True, exist_ok=True)
        args.out_json.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if args.out_md:
        args.out_md.parent.mkdir(parents=True, exist_ok=True)
        args.out_md.write_text(render_markdown(report), encoding="utf-8")
    if args.out_html:
        args.out_html.parent.mkdir(parents=True, exist_ok=True)
        args.out_html.write_text(
            render_html(report, pages_base_url=args.pages_base_url),
            encoding="utf-8",
        )

    print(json.dumps(report["counts"], indent=2))
    print("hasChanges", report["hasChanges"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
