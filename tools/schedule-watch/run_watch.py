#!/usr/bin/env python3
"""
One schedule-watch cycle:
  fetch (or --new-file) → fortress health gates → diff → render site → update snapshot/success marker
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from config_loader import load_config
from validate_snapshot import validate_snapshot

ROOT = Path(__file__).resolve().parent
DEFAULT_SNAPSHOT = ROOT / "data" / "schedule-snapshot.json"
DEFAULT_SUCCESS = ROOT / "data" / "last-success.json"
OUT_DIR = ROOT / "out"


def write_gh_output(**kwargs: object) -> None:
    """Append GitHub Actions outputs using the multiline delimiter form (safe for special chars)."""
    gh_out = Path(os.getenv("GITHUB_OUTPUT", ""))
    if not gh_out.name or str(gh_out) == ".":
        return
    with gh_out.open("a", encoding="utf-8") as f:
        for k, v in kwargs.items():
            val = "" if v is None else str(v)
            f.write(f"{k}<<EOF\n{val}\nEOF\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    ap.add_argument("--last-success", type=Path, default=DEFAULT_SUCCESS)
    ap.add_argument("--new-file", type=Path)
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    ap.add_argument("--pages-dir", type=Path, default=ROOT / "pages")
    ap.add_argument("--pages-base-url", default=os.getenv("PAGES_BASE_URL", ""))
    ap.add_argument("--config", type=Path, default=None)
    ap.add_argument("--min-game-ratio", type=float, default=None)
    ap.add_argument("--always-update-snapshot", action="store_true")
    ap.add_argument("--skip-fetch", action="store_true")
    ap.add_argument("--skip-site", action="store_true")
    args = ap.parse_args()

    cfg = load_config(str(args.config) if args.config else None)
    fortress = cfg["fortress"]
    club_id = int(cfg["club"]["id"])
    min_ratio = (
        float(args.min_game_ratio)
        if args.min_game_ratio is not None
        else float(fortress.get("minGameRatio") or 0.70)
    )
    max_added = int(fortress.get("maxAddedGames") or 0)
    max_removed = int(fortress.get("maxRemovedGames") or 0)
    require_https = bool(fortress.get("requireHttpsPagesUrl", True))

    from render_site import sanitize_pages_base_url

    args.pages_base_url = sanitize_pages_base_url(
        args.pages_base_url, require_https=require_https
    )

    args.out_dir.mkdir(parents=True, exist_ok=True)
    new_path = args.out_dir / "schedule-snapshot-new.json"
    report_json = args.out_dir / "changes.json"
    report_md = args.out_dir / "changes.md"
    report_html = args.out_dir / "changes.html"
    health_path = args.out_dir / "health.json"
    metrics_path = args.out_dir / "fetch-metrics.json"

    if not args.snapshot.exists():
        print(f"Missing baseline snapshot: {args.snapshot}", file=sys.stderr)
        return 2

    old = json.loads(args.snapshot.read_text(encoding="utf-8"))
    if fortress.get("validateSnapshot", True):
        old_errs = validate_snapshot(old, expected_club_id=club_id)
        if old_errs:
            print("Baseline snapshot failed validation:", file=sys.stderr)
            for e in old_errs[:20]:
                print(f"  - {e}", file=sys.stderr)
            write_gh_output(health_ok="false", health_reason="baseline_invalid", has_changes="false")
            return 4

    old_count = len(old.get("games") or [])

    if args.new_file:
        shutil.copy(args.new_file, new_path)
    else:
        if args.skip_fetch:
            print("--skip-fetch requires --new-file", file=sys.stderr)
            return 2
        fetch_cmd = [sys.executable, str(ROOT / "fetch_schedule.py"), "--out", str(new_path)]
        if args.config:
            fetch_cmd += ["--config", str(args.config)]
        rc = subprocess.call(fetch_cmd)
        if rc != 0:
            health = {
                "ok": False,
                "reason": "fetch_failed",
                "oldGameCount": old_count,
                "checkedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            }
            health_path.write_text(json.dumps(health, indent=2) + "\n", encoding="utf-8")
            write_gh_output(health_ok="false", health_reason="fetch_failed", has_changes="false")
            return rc

    new = json.loads(new_path.read_text(encoding="utf-8"))
    if fortress.get("validateSnapshot", True):
        new_errs = validate_snapshot(new, expected_club_id=club_id)
        if new_errs:
            health = {
                "ok": False,
                "reason": "snapshot_invalid",
                "errors": new_errs[:30],
                "oldGameCount": old_count,
                "newGameCount": len(new.get("games") or []),
                "checkedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            }
            health_path.write_text(json.dumps(health, indent=2) + "\n", encoding="utf-8")
            write_gh_output(health_ok="false", health_reason="snapshot_invalid", has_changes="false")
            print("New snapshot failed validation:", file=sys.stderr)
            for e in new_errs[:20]:
                print(f"  - {e}", file=sys.stderr)
            return 4

    new_count = len(new.get("games") or [])
    if new.get("fetchMetrics"):
        metrics_path.write_text(json.dumps(new["fetchMetrics"], indent=2) + "\n", encoding="utf-8")
        print("fetchMetrics", json.dumps(new["fetchMetrics"]))

    # Count-drop / empty fetch guard
    ratio = (new_count / old_count) if old_count else 1.0
    count_drop = old_count > 0 and new_count < max(1, int(old_count * min_ratio))
    reason = "ok"
    if new_count == 0:
        reason = "empty_fetch"
    elif count_drop:
        reason = "count_drop"

    health = {
        "ok": reason == "ok",
        "reason": reason,
        "oldGameCount": old_count,
        "newGameCount": new_count,
        "minRatio": min_ratio,
        "actualRatio": round(ratio, 3),
        "oldProbedAt": old.get("probedAt"),
        "newProbedAt": new.get("probedAt"),
        "clubId": club_id,
        "checkedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }
    health_path.write_text(json.dumps(health, indent=2) + "\n", encoding="utf-8")
    print("health", json.dumps(health))

    if not health["ok"]:
        write_gh_output(
            health_ok="false",
            health_reason=health["reason"],
            has_changes="false",
            old_game_count=old_count,
            new_game_count=new_count,
        )
        print("Refusing to update snapshot due to health failure", file=sys.stderr)
        return 3

    compare_cmd = [
        sys.executable,
        str(ROOT / "compare_snapshots.py"),
        "--old",
        str(args.snapshot),
        "--new",
        str(new_path),
        "--out-json",
        str(report_json),
        "--out-md",
        str(report_md),
        "--out-html",
        str(report_html),
        "--pages-base-url",
        args.pages_base_url,
    ]
    if args.config:
        compare_cmd += ["--config", str(args.config)]
    rc = subprocess.call(compare_cmd)
    if rc != 0:
        write_gh_output(health_ok="false", health_reason="diff_failed", has_changes="false")
        return rc

    report = json.loads(report_json.read_text(encoding="utf-8"))
    added_n = int(report["counts"]["added"])
    removed_n = int(report["counts"]["removed"])

    # Max churn gates — refuse to accept a wild rewrite of the season
    churn_reason = None
    if max_added > 0 and added_n > max_added:
        churn_reason = "max_added"
    elif max_removed > 0 and removed_n > max_removed:
        churn_reason = "max_removed"
    if churn_reason:
        health = {
            "ok": False,
            "reason": churn_reason,
            "oldGameCount": old_count,
            "newGameCount": new_count,
            "added": added_n,
            "removed": removed_n,
            "maxAddedGames": max_added,
            "maxRemovedGames": max_removed,
            "checkedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        }
        health_path.write_text(json.dumps(health, indent=2) + "\n", encoding="utf-8")
        write_gh_output(
            health_ok="false",
            health_reason=churn_reason,
            has_changes="false",
            added=added_n,
            removed=removed_n,
        )
        print(f"Refusing snapshot update: {churn_reason} (added={added_n}, removed={removed_n})", file=sys.stderr)
        return 3

    has = bool(report.get("hasChanges"))
    (args.out_dir / "has_changes.txt").write_text("true\n" if has else "false\n", encoding="utf-8")

    from render_site import next_cron_run, schedule_banner, parse_iso, fmt_stamp

    executed = datetime.now(timezone.utc)
    last_run = parse_iso(new.get("probedAt")) or executed
    next_run = next_cron_run(executed)
    timing_txt = schedule_banner(executed, next_run, as_html=False)
    timing_md = (
        f"\n---\n\n**Last run:** {fmt_stamp(executed)}  \n"
        f"**Next run:** {fmt_stamp(next_run)} (weekly Monday 07:00 UTC ≈ 08:00 Vienna in winter)\n"
    )
    if report_md.exists():
        report_md.write_text(report_md.read_text(encoding="utf-8") + timing_md, encoding="utf-8")
    (args.out_dir / "run-timing.txt").write_text(timing_txt, encoding="utf-8")
    write_gh_output(
        last_run=fmt_stamp(executed),
        next_run=fmt_stamp(next_run),
        last_run_iso=executed.strftime("%Y-%m-%dT%H:%M:%SZ"),
        next_run_iso=next_run.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )

    if not args.skip_site:
        site_cmd = [
            sys.executable,
            str(ROOT / "render_site.py"),
            "--snapshot",
            str(new_path),
            "--out-dir",
            str(args.pages_dir),
            "--pages-base-url",
            args.pages_base_url,
            "--last-success",
            str(args.last_success),
        ]
        if args.config:
            site_cmd += ["--config", str(args.config)]
        rc = subprocess.call(site_cmd)
        if rc != 0:
            write_gh_output(health_ok="false", health_reason="site_render_failed", has_changes=str(has).lower())
            return rc
        site_out = args.out_dir / "site"
        if site_out.exists():
            shutil.rmtree(site_out, ignore_errors=True)
        try:
            shutil.copytree(args.pages_dir, site_out)
        except shutil.Error as e:
            print(f"copytree warning: {e}", file=sys.stderr)
            site_out.mkdir(parents=True, exist_ok=True)
            for src in args.pages_dir.rglob("*"):
                rel = src.relative_to(args.pages_dir)
                dst = site_out / rel
                if src.is_dir():
                    dst.mkdir(parents=True, exist_ok=True)
                else:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(src, dst)

    if has or args.always_update_snapshot:
        shutil.copy(new_path, args.snapshot)
        print(f"Updated baseline snapshot -> {args.snapshot}")
    else:
        print("Baseline snapshot unchanged (no field diffs)")

    success = {
        "probedAt": new.get("probedAt"),
        "gameCount": new_count,
        "clubId": club_id,
        "savedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "fetchMetrics": new.get("fetchMetrics"),
    }
    args.last_success.parent.mkdir(parents=True, exist_ok=True)
    args.last_success.write_text(json.dumps(success, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote last-success -> {args.last_success}")

    write_gh_output(
        health_ok="true",
        health_reason="ok",
        has_changes="true" if has else "false",
        changed=report["counts"]["changed"],
        added=report["counts"]["added"],
        removed=report["counts"]["removed"],
        old_game_count=old_count,
        new_game_count=new_count,
        probed_at=new.get("probedAt") or "",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
