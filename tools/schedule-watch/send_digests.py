#!/usr/bin/env python3
"""
Build and optionally send schedule digests.

Transport (see mailer.py): prefer existing SMTP mailbox (SMTP_*), else Resend.
Credentials must live in GitHub Actions Secrets — never in the repo.

Modes:
  --mode changes   Split change report by coaches.competitionId (+ default ALERT_TO)
  --mode all-clear Healthy Monday with no schedule changes (peace-of-mind heartbeat)

Dry-run (default): write out/digests/*.json + preview HTML/MD, do not send.
Pass --send to deliver when SMTP_* (or RESEND_API_KEY) is set.
"""

from __future__ import annotations

import argparse
import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

from compare_snapshots import render_html, render_markdown
from config_loader import load_config
from mailer import _emails, deliver, mail_ready

ROOT = Path(__file__).resolve().parent


def coaches_map(cfg: dict[str, Any]) -> dict[int, list[str]]:
    """competitionId → emails from config.coaches."""
    raw = cfg.get("coaches") or {}
    out: dict[int, list[str]] = {}
    if not isinstance(raw, dict):
        return out
    for key, val in raw.items():
        try:
            cid = int(key)
        except (TypeError, ValueError):
            continue
        addrs = _emails(val)
        if addrs:
            out[cid] = addrs
    return out


def competition_id_of_event(entry: dict[str, Any]) -> int | None:
    for key in ("game", "after", "before"):
        g = entry.get(key)
        if isinstance(g, dict) and g.get("competitionId") is not None:
            try:
                return int(g["competitionId"])
            except (TypeError, ValueError):
                return None
    return None


def filter_report(report: dict[str, Any], competition_ids: set[int] | None) -> dict[str, Any]:
    """If competition_ids is None, return full report. Else keep matching events only."""
    if competition_ids is None:
        return report
    out = deepcopy(report)

    def keep(entry: dict[str, Any]) -> bool:
        cid = competition_id_of_event(entry)
        return cid is not None and cid in competition_ids

    for key in ("added", "removed", "changed", "cancellations"):
        out[key] = [e for e in (report.get(key) or []) if keep(e)]
    out["counts"] = {
        "added": len(out["added"]),
        "removed": len(out["removed"]),
        "changed": len(out["changed"]),
        "cancellations": len(out["cancellations"]),
        "totalEvents": len(out["added"]) + len(out["removed"]) + len(out["changed"]),
    }
    out["hasChanges"] = bool(out["added"] or out["removed"] or out["changed"])
    out["filterCompetitionIds"] = sorted(competition_ids)
    return out


def build_recipient_buckets(
    report: dict[str, Any],
    cfg: dict[str, Any],
    default_to: list[str],
) -> list[dict[str, Any]]:
    """
    One bucket per unique recipient set.
    - Mapped competitions → those coaches only
    - Unmapped competitions → default ALERT_TO
    - If no coaches map at all → single default digest (full report)
    """
    cmap = coaches_map(cfg)
    if not cmap:
        if not default_to:
            return []
        return [
            {
                "key": "default",
                "to": default_to,
                "label": "all competitions",
                "competitionIds": None,
                "report": report,
            }
        ]

    # competition ids present in the change set
    touched: dict[int, list[str]] = {}
    for key in ("added", "removed", "changed"):
        for entry in report.get(key) or []:
            cid = competition_id_of_event(entry)
            if cid is None:
                continue
            touched.setdefault(cid, [])

    # Group competition ids by frozenset of recipients
    groups: dict[frozenset[str], set[int]] = {}
    unmapped: set[int] = set()
    for cid in touched:
        addrs = cmap.get(cid)
        if not addrs:
            unmapped.add(cid)
            continue
        groups.setdefault(frozenset(addrs), set()).add(cid)

    buckets: list[dict[str, Any]] = []
    for addrs_fs, cids in sorted(groups.items(), key=lambda x: sorted(x[1])[0]):
        filt = filter_report(report, cids)
        if not filt["hasChanges"]:
            continue
        label = "competitions " + ", ".join(str(c) for c in sorted(cids))
        buckets.append(
            {
                "key": "coach-" + "-".join(str(c) for c in sorted(cids)),
                "to": sorted(addrs_fs),
                "label": label,
                "competitionIds": sorted(cids),
                "report": filt,
            }
        )

    if unmapped and default_to:
        filt = filter_report(report, unmapped)
        if filt["hasChanges"]:
            buckets.append(
                {
                    "key": "default-unmapped",
                    "to": default_to,
                    "label": "unmapped competitions " + ", ".join(str(c) for c in sorted(unmapped)),
                    "competitionIds": sorted(unmapped),
                    "report": filt,
                }
            )
    elif unmapped and not default_to:
        # No default inbox — fold unmapped into nothing; warn via empty skip
        pass

    # Also: if default_to set and we want them to always get a full club digest,
    # controlled by config.coachesNotifyDefaultFull (default False = split only).
    if cfg.get("coachesNotifyDefaultFull") and default_to and cmap:
        buckets.append(
            {
                "key": "default-full",
                "to": default_to,
                "label": "full club digest",
                "competitionIds": None,
                "report": report,
            }
        )

    return buckets


def all_clear_bodies(
    club_name: str,
    health: dict[str, Any],
    pages_base_url: str,
    timing: str,
) -> tuple[str, str, str]:
    subject = f"[{club_name}] schedule watch OK — no changes"
    base = (pages_base_url or "").rstrip("/")
    links_txt = ""
    links_html = ""
    if base.startswith("https://") or base.startswith("http://"):
        links_txt = (
            f"\nSite:\n- Home: {base}/\n- Weekend: {base}/weekend.html\n"
            f"- Clashes: {base}/clashes.html\n- Calendar: {base}/calendar.html\n"
        )
        links_html = f"""
<p style="margin:12px 0 0;font:14px/1.45 Arial,sans-serif">
  <a href="{base}/">Home</a> ·
  <a href="{base}/weekend.html">Weekend</a> ·
  <a href="{base}/clashes.html">Clashes</a> ·
  <a href="{base}/calendar.html">Calendar</a>
</p>"""
    text = (
        f"{club_name} Monday schedule watch completed successfully.\n\n"
        f"No schedule changes detected.\n"
        f"- games: {health.get('newGameCount', health.get('oldGameCount', '?'))}\n"
        f"- probedAt: {health.get('newProbedAt') or health.get('oldProbedAt')}\n"
        f"- health: {health.get('reason', 'ok')}\n\n"
        f"{timing}{links_txt}"
        f"This is the weekly all-clear heartbeat (peace of mind). "
        f"Change digests are sent separately when something moves.\n"
    )
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{club_name} schedule OK</title></head>
<body style="margin:0;padding:0;background:#ececec">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#ececec;padding:16px 0">
<tr><td align="center">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#ffffff">
  <tr><td style="background:#0d0d0d;padding:16px 18px">
    <div style="font:700 11px/1 Arial,sans-serif;letter-spacing:.14em;text-transform:uppercase;color:#ccff00">{club_name}</div>
    <div style="font:700 20px/1.2 Arial,sans-serif;color:#ffffff;margin-top:6px">Schedule watch OK</div>
  </td></tr>
  <tr><td style="padding:16px 18px;font:14px/1.45 Arial,sans-serif;color:#111">
    <p style="margin:0 0 10px">No schedule changes this week.</p>
    <p style="margin:0;color:#555;font-size:13px">
      Games: {health.get('newGameCount', health.get('oldGameCount', '—'))}<br>
      Probed: {health.get('newProbedAt') or health.get('oldProbedAt') or '—'}<br>
      Health: {health.get('reason', 'ok')}
    </p>
    <pre style="margin:12px 0 0;font:12px/1.4 monospace;color:#666;white-space:pre-wrap">{timing.strip()}</pre>
    {links_html}
    <p style="margin:16px 0 0;font:12px/1.4 Arial,sans-serif;color:#888">
      Weekly all-clear heartbeat. Change digests are separate.
    </p>
  </td></tr>
</table>
</td></tr></table>
</body></html>
"""
    return subject, text, html


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("changes", "all-clear"), required=True)
    ap.add_argument("--changes-json", type=Path, default=ROOT / "out" / "changes.json")
    ap.add_argument("--health-json", type=Path, default=ROOT / "out" / "health.json")
    ap.add_argument("--timing", type=Path, default=ROOT / "out" / "run-timing.txt")
    ap.add_argument("--out-dir", type=Path, default=ROOT / "out" / "digests")
    ap.add_argument("--pages-base-url", default=os.getenv("PAGES_BASE_URL", ""))
    ap.add_argument("--config", type=Path, default=None)
    ap.add_argument("--send", action="store_true", help="Deliver via SMTP (or Resend if SMTP unset)")
    args = ap.parse_args()

    cfg = load_config(str(args.config) if args.config else None)
    club_name = (cfg.get("club") or {}).get("name") or "Club"
    default_to = _emails(os.getenv("ALERT_TO") or (cfg.get("alertTo") or ""))
    heartbeat_to = _emails(os.getenv("HEARTBEAT_TO") or "") or default_to
    pages = args.pages_base_url or os.getenv("PAGES_BASE_URL") or ""
    timing = ""
    if args.timing.exists():
        timing = args.timing.read_text(encoding="utf-8")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, Any]] = []

    if args.mode == "all-clear":
        health = {}
        if args.health_json.exists():
            health = json.loads(args.health_json.read_text(encoding="utf-8"))
        if not heartbeat_to:
            print("No HEARTBEAT_TO/ALERT_TO — skipping all-clear")
            return 0
        subject, text, html = all_clear_bodies(club_name, health, pages, timing)
        preview = {
            "mode": "all-clear",
            "to": heartbeat_to,
            "subject": subject,
            "text": text,
            "html": html,
        }
        (args.out_dir / "all-clear.json").write_text(
            json.dumps({k: preview[k] for k in ("mode", "to", "subject")} | {"text": text}, indent=2)
            + "\n",
            encoding="utf-8",
        )
        (args.out_dir / "all-clear.html").write_text(html, encoding="utf-8")
        (args.out_dir / "all-clear.md").write_text(text, encoding="utf-8")
        manifest.append({"key": "all-clear", "to": heartbeat_to, "subject": subject})
        if args.send:
            if not mail_ready():
                print("SMTP_* (or RESEND_API_KEY) missing — wrote preview only")
            else:
                print("sent all-clear", deliver(to=heartbeat_to, subject=subject, text=text, html=html))
        else:
            print(f"dry-run all-clear → {heartbeat_to} (use --send to deliver)")
        (args.out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        return 0

    # changes mode
    if not args.changes_json.exists():
        print(f"Missing {args.changes_json}", flush=True)
        return 2
    report = json.loads(args.changes_json.read_text(encoding="utf-8"))
    if not report.get("hasChanges"):
        print("No changes in report — nothing to digest")
        return 0

    buckets = build_recipient_buckets(report, cfg, default_to)
    if not buckets:
        print("No recipients (set ALERT_TO and/or config.coaches) — wrote nothing")
        return 0

    for b in buckets:
        filt = b["report"]
        md = render_markdown(filt)
        if pages:
            md += (
                f"\n\n---\nOpen the schedule site:\n- Home: {pages.rstrip('/')}/\n"
                f"- Weekend: {pages.rstrip('/')}/weekend.html\n"
                f"- Clashes: {pages.rstrip('/')}/clashes.html\n"
                f"- Calendar: {pages.rstrip('/')}/calendar.html\n"
            )
        html = render_html(filt, pages_base_url=pages)
        c = filt["counts"]
        subject = (
            f"[{club_name}] {c['changed']} changed, {c['added']} added, {c['removed']} removed"
            f" ({b['label']})"
        )
        text = (timing + "\n" if timing else "") + md
        key = b["key"]
        (args.out_dir / f"{key}.md").write_text(text, encoding="utf-8")
        (args.out_dir / f"{key}.html").write_text(html, encoding="utf-8")
        meta = {
            "key": key,
            "to": b["to"],
            "label": b["label"],
            "competitionIds": b["competitionIds"],
            "counts": c,
            "subject": subject,
        }
        (args.out_dir / f"{key}.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        manifest.append(meta)
        print(f"digest {key} → {b['to']} counts={c}")
        if args.send:
            if not mail_ready():
                print("SMTP_* (or RESEND_API_KEY) missing — preview only")
            else:
                print("sent", deliver(to=b["to"], subject=subject, text=text, html=html))

    (args.out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    if not args.send:
        print(f"dry-run wrote {len(manifest)} digest(s) under {args.out_dir} (use --send to deliver)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
