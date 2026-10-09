#!/usr/bin/env python3
"""Fetch club league schedule via FloorballFlash MCP (streamable HTTP)."""

from __future__ import annotations

import argparse
import http.client
import json
import re
import ssl
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config_loader import load_config


class MCPClient:
    def __init__(self, host: str, timeout: int = 180) -> None:
        self.host = host
        self.timeout = timeout
        self.ctx = ssl.create_default_context()
        self.conn: http.client.HTTPSConnection | None = None
        self.session: str | None = None
        self._initialized = False
        self._req = 0
        self.metrics = {
            "calls": 0,
            "successes": 0,
            "retries": 0,
            "sessionNotFound": 0,
            "httpErrors": 0,
            "connectionErrors": 0,
            "reinitializes": 0,
            "byTool": {},
        }
        self._connect()

    def _connect(self) -> None:
        if self.conn:
            try:
                self.conn.close()
            except Exception:
                pass
        self.conn = http.client.HTTPSConnection(self.host, context=self.ctx, timeout=self.timeout)
        self.session = None
        self._initialized = False

    def _post(self, payload: dict[str, Any]) -> tuple[int, str]:
        assert self.conn is not None
        body = json.dumps(payload).encode()
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Connection": "keep-alive",
        }
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        try:
            self.conn.request("POST", "/", body, headers)
            resp = self.conn.getresponse()
        except Exception as e:
            self._connect()
            raise ConnectionError(str(e)) from e
        newsess = resp.getheader("Mcp-Session-Id")
        if newsess:
            self.session = newsess
        return resp.status, resp.read().decode()

    def _parse_rpc(self, data: str) -> dict[str, Any] | None:
        """Parse JSON-RPC from plain JSON or SSE `data:` lines."""
        text = (data or "").strip()
        if not text:
            return None
        if text.startswith("{"):
            try:
                return json.loads(text)
            except Exception:
                pass
        result = None
        for line in text.splitlines():
            if line.startswith("data: "):
                try:
                    result = json.loads(line[6:])
                except Exception:
                    continue
        return result

    def initialize(self) -> str | None:
        """Initialize MCP session. Session id is optional (some hosts omit it)."""
        self._connect()
        self._initialized = False
        status, data = self._post(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "schedule-watch", "version": "2.1"},
                },
            }
        )
        if status != 200:
            raise RuntimeError(f"init failed {status} {data[:200]}")
        parsed = self._parse_rpc(data)
        if not parsed or "error" in parsed or "result" not in parsed:
            raise RuntimeError(f"init bad body {data[:200]}")
        # Optional notifications/initialized (best-effort; ignore failures)
        try:
            self._post(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/initialized",
                    "params": {},
                }
            )
        except Exception:
            pass
        self._initialized = True
        return self.session

    def call_tool(self, name: str, arguments: dict[str, Any], max_attempts: int = 20) -> str:
        last = None
        self.metrics["calls"] += 1
        tool_m = self.metrics["byTool"].setdefault(name, {"calls": 0, "retries": 0, "successes": 0})
        tool_m["calls"] += 1
        for attempt in range(max_attempts):
            self._req += 1
            try:
                if not getattr(self, "_initialized", False):
                    self.initialize()
                    self.metrics["reinitializes"] += 1
                status, data = self._post(
                    {
                        "jsonrpc": "2.0",
                        "id": self._req,
                        "method": "tools/call",
                        "params": {"name": name, "arguments": arguments},
                    }
                )
            except ConnectionError as e:
                last = str(e)
                self.metrics["connectionErrors"] += 1
                self.metrics["retries"] += 1
                tool_m["retries"] += 1
                self._initialized = False
                try:
                    self.initialize()
                    self.metrics["reinitializes"] += 1
                except Exception:
                    pass
                time.sleep(min(8.0, 0.2 * (2 ** attempt)))
                continue
            if status == 404 or "session not found" in data.lower():
                last = "404"
                self.metrics["sessionNotFound"] += 1
                self.metrics["retries"] += 1
                tool_m["retries"] += 1
                self._initialized = False
                self.session = None
                try:
                    self.initialize()
                    self.metrics["reinitializes"] += 1
                except Exception:
                    pass
                time.sleep(min(8.0, 0.15 * (2 ** attempt)))
                continue
            if status != 200:
                last = f"{status}:{data[:80]}"
                self.metrics["httpErrors"] += 1
                self.metrics["retries"] += 1
                tool_m["retries"] += 1
                self._initialized = False
                try:
                    self.initialize()
                    self.metrics["reinitializes"] += 1
                except Exception:
                    pass
                time.sleep(min(8.0, 0.2 * (2 ** attempt)))
                continue
            result = self._parse_rpc(data)
            if not result or "error" in result:
                last = str(result)[:200] if result else "no-json"
                self.metrics["retries"] += 1
                tool_m["retries"] += 1
                time.sleep(min(4.0, 0.1 * (2 ** attempt)))
                continue
            texts = [
                c.get("text", "")
                for c in result.get("result", {}).get("content", [])
                if c.get("type") == "text"
            ]
            self.metrics["successes"] += 1
            tool_m["successes"] += 1
            return "\n".join(texts)
        raise RuntimeError(f"{name}({arguments}) failed after {max_attempts} attempts: {last}")


SUMMARY_PAT = re.compile(
    r"^#(\d+)\s+(.+?)\s+(\d+):(\d+)\s+(.+?)\s+\[(\d{4}-\d{2}-\d{2})\]\s*$"
)
COMP_PAT = re.compile(
    r"^- (.+?) \((\d{4}), ([^,]+), ([^,]+), ID: (\d+)\) — organizer (.+)$"
)
DETAIL = {
    "header": re.compile(
        r"^#(\d+)\s+(.+?)\s+(\d+):(\d+)\s+(.+?)\s+\[(\d{4}-\d{2}-\d{2})\]\s*$"
    ),
    "state": re.compile(r"^State:\s*(.+)$", re.M),
    "date": re.compile(r"^Date:\s*(.+)$", re.M),
    "time": re.compile(r"^Time:\s*(.+)$", re.M),
    "venue": re.compile(r"^Venue:\s*(.+)$", re.M),
    "competition": re.compile(r"^Competition:\s*(.+?)\s*\(ID:\s*(\d+)\)\s*$", re.M),
}


def parse_summaries(text: str) -> list[dict[str, Any]]:
    out = []
    for ln in text.splitlines():
        m = SUMMARY_PAT.match(ln.strip())
        if not m:
            continue
        out.append(
            {
                "id": int(m.group(1)),
                "homeTeam": m.group(2),
                "homeScore": int(m.group(3)),
                "awayScore": int(m.group(4)),
                "awayTeam": m.group(5),
                "date": m.group(6),
            }
        )
    return out


def parse_detail(text: str) -> dict[str, Any]:
    d: dict[str, Any] = {"raw": text}
    lines = text.strip().splitlines()
    if lines:
        m = DETAIL["header"].match(lines[0].strip())
        if m:
            d.update(
                {
                    "id": int(m.group(1)),
                    "homeTeam": m.group(2),
                    "homeScore": int(m.group(3)),
                    "awayScore": int(m.group(4)),
                    "awayTeam": m.group(5),
                    "date": m.group(6),
                }
            )
    for key in ("state", "date", "time", "venue"):
        m = DETAIL[key].search(text)
        if m:
            d[key] = m.group(1).strip()
    m = DETAIL["competition"].search(text)
    if m:
        d["competitionName"] = m.group(1).strip()
        d["competitionId"] = int(m.group(2))
    return d


def ha_and_opponent(home: str, away: str, matchers: list[str]) -> tuple[str, str, str | None]:
    needles = [m.lower() for m in matchers] or ["floorballbunnies"]

    def is_club(name: str) -> bool:
        low = name.lower()
        return any(n in low or low.startswith(n) for n in needles)

    home_b, away_b = is_club(home), is_club(away)
    if home_b and not away_b:
        return "Home", away, home
    if away_b and not home_b:
        return "Away", home, away
    if home_b and away_b:
        return "Both-club?", f"{home} vs {away}", home
    return "Unknown", f"{home} vs {away}", None


def fetch_snapshot(club_id: int | None = None, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = cfg or load_config()
    club = cfg["club"]
    mcp_cfg = cfg["mcp"]
    club_id = int(club_id if club_id is not None else club["id"])
    matchers = list(club.get("nameMatchers") or [club["name"]])
    watch_fields = list(cfg["schedule"]["watchFields"])
    max_attempts = int(mcp_cfg.get("maxAttempts") or 20)

    mcp = MCPClient(str(mcp_cfg["host"]), timeout=int(mcp_cfg.get("timeoutSeconds") or 180))
    mcp.initialize()
    season_text = mcp.call_tool("current_season", {}, max_attempts=max_attempts)
    m = re.search(r"(\d{4})", season_text)
    season = int(m.group(1)) if m else datetime.now(timezone.utc).year

    comps_text = mcp.call_tool(
        "search_competitions", {"season": season, "limit": 500}, max_attempts=max_attempts
    )
    competitions = []
    for ln in comps_text.splitlines():
        cm = COMP_PAT.match(ln.strip())
        if cm:
            competitions.append(
                {
                    "name": cm.group(1),
                    "season": int(cm.group(2)),
                    "type": cm.group(3),
                    "gender": cm.group(4),
                    "id": int(cm.group(5)),
                    "organizer": cm.group(6),
                }
            )

    games_by_id: dict[int, dict[str, Any]] = {}
    for c in competitions:
        if c["type"] != "League":
            continue
        text = mcp.call_tool(
            "search_games",
            {"clubId": club_id, "competitionId": c["id"], "limit": 200},
            max_attempts=max_attempts,
        )
        for g in parse_summaries(text):
            g["competitionFromSearch"] = c
            games_by_id[g["id"]] = g

    enriched = []
    for i, gid in enumerate(sorted(games_by_id)):
        g = games_by_id[gid]
        detail_text = mcp.call_tool("get_game", {"id": gid}, max_attempts=max_attempts)
        detail = parse_detail(detail_text)
        home = detail.get("homeTeam") or g.get("homeTeam")
        away = detail.get("awayTeam") or g.get("awayTeam")
        ha, opp, bname = ha_and_opponent(home or "", away or "", matchers)
        cid = detail.get("competitionId") or g.get("competitionFromSearch", {}).get("id")
        cname = detail.get("competitionName") or g.get("competitionFromSearch", {}).get("name")
        meta = next((c for c in competitions if c["id"] == cid), g.get("competitionFromSearch"))
        if meta and meta.get("type") and meta.get("type") != "League":
            continue
        enriched.append(
            {
                "id": gid,
                "date": detail.get("date") or g.get("date"),
                "time": detail.get("time"),
                "venue": detail.get("venue"),
                "state": detail.get("state"),
                "homeTeam": home,
                "awayTeam": away,
                "homeAway": ha,
                "opponent": opp,
                "clubTeamName": bname,
                "competitionId": cid,
                "competitionName": cname,
            }
        )
        if (i + 1) % 40 == 0:
            print(f"enriched {i+1}/{len(games_by_id)}", flush=True)

    probed = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return {
        "schemaVersion": 1,
        "clubId": club_id,
        "clubName": club["name"],
        "clubSlug": club.get("slug"),
        "currentSeason": season,
        "probedAt": probed,
        "source": "floorballflash-mcp",
        "timezone": cfg["schedule"].get("timezone") or "Europe/Vienna",
        "watchFields": watch_fields,
        "games": enriched,
        "gameCount": len(enriched),
        "competitionsChecked": competitions,
        "fetchMetrics": mcp.metrics,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--club-id", type=int, default=None)
    ap.add_argument("--config", type=Path, default=None)
    args = ap.parse_args()
    cfg = load_config(str(args.config) if args.config else None)
    snap = fetch_snapshot(args.club_id, cfg)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(snap, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {args.out} games={len(snap['games'])} probedAt={snap['probedAt']}")
    print("fetchMetrics", json.dumps(snap.get("fetchMetrics") or {}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
