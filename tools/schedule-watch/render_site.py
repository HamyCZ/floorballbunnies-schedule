#!/usr/bin/env python3
"""Build static GitHub Pages site (branded) from schedule snapshots."""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from config_loader import load_config

BAND_ORDER = ["U8", "U10", "U12", "U14", "U17", "Adults"]
# Club squads / calendar categories — finer than age band.
# Assist and Bully stay separate calendars (B team / A team); Alpencup is its own squad.
CATEGORY_ORDER = [
    "U8",
    "U10",
    "U10 Alpencup",
    "U12 Assist",
    "U12 Bully",
    "U12 Mädchen",
    "U12 Alpencup",
    "U14 Großfeld",
    "U14 Kleinfeld",
    "U14w",
    "U17",
    "U17w",
    "Adults Grossfeld",
    "Adults Kleinfeld",
    "Bundesliga",
]

# Related squads: still separate categories/calendars, but surface cross-squad overlaps.
# U12: Bully = A, Assist = B, Mädchen, Alpencup (OÖ) — same-day only awareness.
RELATED_SQUAD_GROUPS = [
    {
        "id": "u12-groups",
        "label": "U12 group overlap",
        "detail": "Assist · Bully · Mädchen · Alpencup — separate squads, flagged when any two play the same day",
        "categories": ("U12 Assist", "U12 Bully", "U12 Mädchen", "U12 Alpencup"),
    },
]
BAND_ACCENT = {
    "U8": "#9ae66e",
    "U10": "#7dd3fc",
    "U12": "#ccff00",
    "U14": "#f5a524",
    "U17": "#f472b6",
    "Adults": "#fb7185",
}
CATEGORY_ACCENT = {
    "U8": "#9ae66e",
    "U10": "#7dd3fc",
    "U10 Alpencup": "#38bdf8",
    "U12 Assist": "#ccff00",
    "U12 Bully": "#a3e635",
    "U12 Mädchen": "#f0abfc",
    "U12 Alpencup": "#bef264",
    "U14 Großfeld": "#f5a524",
    "U14 Kleinfeld": "#fb923c",
    "U14w": "#fdba74",
    "U17": "#f472b6",
    "U17w": "#e879f9",
    "Adults Grossfeld": "#fb7185",
    "Adults Kleinfeld": "#f87171",
    "Bundesliga": "#ef4444",
}

CRON_WEEKDAY = 0  # Monday
CRON_HOUR_UTC = 7
CRON_MINUTE_UTC = 0

# Europe/Vienna VTIMEZONE block (CET/CEST EU rules) for ICS
VIENNA_VTIMEZONE = """BEGIN:VTIMEZONE
TZID:Europe/Vienna
X-LIC-LOCATION:Europe/Vienna
BEGIN:DAYLIGHT
TZOFFSETFROM:+0100
TZOFFSETTO:+0200
TZNAME:CEST
DTSTART:19700329T020000
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU
END:DAYLIGHT
BEGIN:STANDARD
TZOFFSETFROM:+0200
TZOFFSETTO:+0100
TZNAME:CET
DTSTART:19701025T030000
RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU
END:STANDARD
END:VTIMEZONE"""

# Module-level active profile (set by render_all / main)
_ACTIVE_CFG: dict[str, Any] | None = None


def active_cfg() -> dict[str, Any]:
    global _ACTIVE_CFG
    if _ACTIVE_CFG is None:
        _ACTIVE_CFG = load_config()
    return _ACTIVE_CFG


def set_active_cfg(cfg: dict[str, Any] | None) -> None:
    global _ACTIVE_CFG
    _ACTIVE_CFG = cfg


def club_info() -> dict[str, Any]:
    return dict(active_cfg().get("club") or {})


def brand_info() -> dict[str, Any]:
    return dict(active_cfg().get("brand") or {"lime": "#ccff00", "ink": "#0d0d0d"})


def ics_uid(*parts: object) -> str:
    """URN-style event UID (not email-shaped): urn:{club-slug}:{part}:…"""
    slug = club_info().get("slug") or "club"
    body = ":".join(str(p) for p in parts if p is not None and str(p) != "")
    return f"urn:{slug}:{body}"


def schedule_timezone() -> str:
    return str(active_cfg().get("schedule", {}).get("timezone") or "Europe/Vienna")


def sanitize_pages_base_url(url: str, *, require_https: bool | None = None) -> str:
    """Allow only http(s) absolute URLs for links embedded in pages/email."""
    u = (url or "").strip().rstrip("/")
    if not u:
        return ""
    low = u.lower()
    if require_https is None:
        require_https = bool(active_cfg().get("fortress", {}).get("requireHttpsPagesUrl", True))
    if require_https:
        if not low.startswith("https://"):
            return ""
    elif not (low.startswith("https://") or low.startswith("http://")):
        return ""
    if any(c in u for c in ('"', "'", "<", ">", " ", "\n", "\r", "`")):
        return ""
    return u


def parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def fmt_stamp(dt: datetime | None) -> str:
    if not dt:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%a %d %b %Y %H:%M") + " UTC"


def next_cron_run(after: datetime | None = None) -> datetime:
    after = after or datetime.now(timezone.utc)
    if after.tzinfo is None:
        after = after.replace(tzinfo=timezone.utc)
    after = after.astimezone(timezone.utc)
    days_ahead = (CRON_WEEKDAY - after.weekday()) % 7
    candidate = (after + timedelta(days=days_ahead)).replace(
        hour=CRON_HOUR_UTC, minute=CRON_MINUTE_UTC, second=0, microsecond=0
    )
    if candidate <= after:
        candidate += timedelta(days=7)
    return candidate


def schedule_banner(
    last_run: datetime | None,
    next_run: datetime | None = None,
    *,
    as_html: bool = True,
) -> str:
    next_run = next_run or next_cron_run(last_run or datetime.now(timezone.utc))
    last_s = fmt_stamp(last_run)
    next_s = fmt_stamp(next_run)
    if as_html:
        return (
            f'<div class="when" role="status">'
            f'<div><span class="lbl">Last run</span> {escape(last_s)}</div>'
            f'<div><span class="lbl">Next run</span> {escape(next_s)}'
            f'<span class="hint"> · Mon 07:00 UTC ≈ 08:00 Vienna (winter)</span></div>'
            f"</div>"
        )
    return (
        f"Last run: {last_s}\n"
        f"Next run: {next_s} (weekly Monday 07:00 UTC ≈ 08:00 Vienna in winter)\n"
    )


def age_band(name: str | None) -> str | None:
    n = name or ""
    if re.search(r"U\s*8\b", n, re.I):
        return "U8"
    if re.search(r"U\s*10\b", n, re.I):
        return "U10"
    if re.search(r"U\s*12", n, re.I):
        return "U12"
    if re.search(r"U\s*14", n, re.I):
        return "U14"
    if re.search(r"U\s*17", n, re.I):
        return "U17"
    if re.search(r"Bundesliga|Grossfeldliga|Großfeldliga|Kleinfeldliga|Herren", n, re.I):
        return "Adults"
    return None


def category_of(name: str | None) -> str | None:
    """Squad / calendar category. Assist and Bully are different teams."""
    n = name or ""
    # Girls / women before generic U-codes
    if re.search(r"U\s*14w\b|U14w", n, re.I) or (
        re.search(r"U\s*14", n, re.I) and re.search(r"Mädchen|Madchen", n, re.I)
    ):
        return "U14w"
    if re.search(r"U\s*17w\b|U17w", n, re.I) or (
        re.search(r"U\s*17", n, re.I) and re.search(r"Mädchen|Madchen", n, re.I)
    ):
        return "U17w"
    if re.search(r"U\s*12", n, re.I) and re.search(r"Mädchen|Madchen", n, re.I):
        return "U12 Mädchen"
    if re.search(r"U\s*12", n, re.I) and re.search(r"Assist", n, re.I):
        return "U12 Assist"
    if re.search(r"U\s*12", n, re.I) and re.search(r"Bully", n, re.I):
        return "U12 Bully"
    if re.search(r"U\s*12", n, re.I) and re.search(r"Alpencup|OÖ", n, re.I):
        return "U12 Alpencup"
    if re.search(r"U\s*8\b", n, re.I):
        return "U8"
    if re.search(r"U\s*10", n, re.I) and re.search(r"Alpencup|OÖ", n, re.I):
        return "U10 Alpencup"
    if re.search(r"U\s*10", n, re.I):
        return "U10"
    if re.search(r"U\s*14", n, re.I) and re.search(r"Kleinfeld", n, re.I):
        return "U14 Kleinfeld"
    if re.search(r"U\s*14", n, re.I):
        return "U14 Großfeld"
    if re.search(r"U\s*17", n, re.I):
        return "U17"
    if re.search(r"Bundesliga", n, re.I):
        return "Bundesliga"
    if re.search(r"Grossfeldliga|Großfeldliga", n, re.I):
        return "Adults Grossfeld"
    if re.search(r"Kleinfeldliga", n, re.I):
        return "Adults Kleinfeld"
    return None


def category_slug(cat: str) -> str:
    s = cat.lower()
    s = s.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "category"


def parse_time(t: str | None) -> int | None:
    if not t:
        return None
    try:
        p = t.strip().split(":")
        return int(p[0]) * 60 + int(p[1])
    except Exception:
        return None


def fmt_date(ds: str) -> str:
    try:
        return datetime.strptime(ds, "%Y-%m-%d").strftime("%a %d %b %Y")
    except Exception:
        return ds


def next_weekend(today: date | None = None) -> tuple[date, date]:
    """Next/current Sat–Sun (kept for any callers that still need weekend bounds)."""
    today = today or date.today()
    wd = today.weekday()
    if wd == 5:
        sat = today
    elif wd == 6:
        sat = today - timedelta(days=1)
    else:
        sat = today + timedelta(days=(5 - wd))
    return sat, sat + timedelta(days=1)


def current_week(today: date | None = None) -> tuple[date, date]:
    """Monday–Sunday of the week containing `today` (Europe/Vienna calendar week)."""
    today = today or date.today()
    mon = today - timedelta(days=today.weekday())
    return mon, mon + timedelta(days=6)


def load_games(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    games = []
    for g in snapshot.get("games") or []:
        if (g.get("state") or "") == "Finished":
            continue
        band = age_band(g.get("competitionName"))
        cat = category_of(g.get("competitionName"))
        if not band or not cat:
            continue
        gg = dict(g)
        gg["_band"] = band
        gg["_category"] = cat
        games.append(gg)
    return games


SITE_CSS = """
:root{
  --ink:#0d0d0d;
  --ink-2:#161616;
  --ink-3:#1f1f1f;
  --line:rgba(204,255,0,.18);
  --lime:#ccff00;
  --lime-2:#bbee00;
  --lime-dim:rgba(204,255,0,.12);
  --text:#f7fafc;
  --muted:#a3a9b0;
  --danger:#ff4d4d;
  --warn:#ff9f1a;
  --radius:14px;
  --font-display:"Oswald","Barlow Condensed","Arial Narrow",sans-serif;
  --font-body:"Barlow","Segoe UI",sans-serif;
}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{
  margin:0;min-height:100vh;color:var(--text);
  font:16px/1.5 var(--font-body);
  background:
    radial-gradient(1200px 600px at 10% -10%, rgba(204,255,0,.16), transparent 55%),
    radial-gradient(900px 500px at 100% 0%, rgba(229,57,53,.12), transparent 50%),
    linear-gradient(180deg, #111 0%, var(--ink) 40%, #090909 100%);
}
a{color:var(--lime);text-decoration:none}
a:hover{color:#e6ff66}
.shell{max-width:980px;margin:0 auto;padding:20px 16px 48px}
.top{
  display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:14px;
  padding:14px 4px 18px;animation:rise .5s ease both;
  position:sticky;top:0;z-index:20;
  background:linear-gradient(180deg, rgba(13,13,13,.97), rgba(13,13,13,.92));
  backdrop-filter:blur(8px);border-bottom:1px solid rgba(204,255,0,.12);
  margin:0 -16px;padding-left:16px;padding-right:16px;
}
.brand{display:flex;align-items:center;gap:14px;min-width:0}
.brand img{
  width:56px;height:56px;border-radius:50%;
  border:2px solid var(--lime);background:#000;
  box-shadow:0 0 0 4px rgba(204,255,0,.12);
}
.brand-text{min-width:0}
.brand-kicker{
  font:700 11px/1 var(--font-display);letter-spacing:.18em;text-transform:uppercase;color:var(--lime);
}
.brand-name{
  font:700 clamp(1.5rem,4vw,2.1rem)/1.05 var(--font-display);
  letter-spacing:.04em;text-transform:uppercase;margin:4px 0 0;
}
.nav{
  display:flex;flex-wrap:wrap;gap:8px;
}
.nav a{
  display:inline-flex;align-items:center;gap:6px;
  padding:10px 14px;border:1px solid var(--line);border-radius:999px;
  color:var(--text);font:600 13px/1 var(--font-display);letter-spacing:.08em;text-transform:uppercase;
  background:rgba(255,255,255,.03);transition:background .2s,border-color .2s,transform .2s;
}
.nav a:hover,.nav a.is-active{
  background:var(--lime);color:#000;border-color:var(--lime);transform:translateY(-1px);
}
details.day-clash{
  margin:0 0 8px;border:1px solid rgba(255,255,255,.08);border-radius:10px;
  background:rgba(0,0,0,.22);overflow:hidden;
}
details.day-clash>summary{
  list-style:none;cursor:pointer;padding:12px 14px;display:flex;align-items:center;justify-content:space-between;
  font:700 14px/1.2 var(--font-display);letter-spacing:.04em;text-transform:uppercase;gap:10px;
}
details.day-clash>summary::-webkit-details-marker{display:none}
details.day-clash>summary .chev{color:var(--lime);transition:transform .2s;flex:0 0 auto}
details.day-clash[open]>summary .chev{transform:rotate(90deg)}
details.day-clash .day-clash-body{padding:0 6px 8px}
.hero{
  position:relative;overflow:hidden;border:1px solid var(--line);border-radius:22px;
  background:
    linear-gradient(135deg, rgba(204,255,0,.12), transparent 42%),
    linear-gradient(180deg, #1a1a1a, #0f0f0f);
  padding:28px 24px 24px;margin:4px 0 18px;animation:rise .55s .05s ease both;
}
.hero::after{
  content:"";position:absolute;inset:auto -20% -40% 40%;height:180px;
  background:radial-gradient(circle, rgba(204,255,0,.22), transparent 65%);
  pointer-events:none;
}
.hero h1{
  position:relative;margin:0;font:700 clamp(1.8rem,5vw,2.8rem)/1 var(--font-display);
  letter-spacing:.03em;text-transform:uppercase;max-width:16ch;
}
.hero p{position:relative;margin:12px 0 0;color:var(--muted);max-width:46ch;font-size:1.02rem}
.cta-row{position:relative;display:flex;flex-wrap:wrap;gap:10px;margin-top:18px}
.btn{
  display:inline-flex;align-items:center;justify-content:center;gap:8px;
  padding:12px 18px;border-radius:999px;font:700 13px/1 var(--font-display);
  letter-spacing:.1em;text-transform:uppercase;border:1px solid transparent;transition:transform .2s,background .2s;
}
.btn:hover{transform:translateY(-1px)}
.btn-lime{background:var(--lime);color:#000}
.btn-lime:hover{background:var(--lime-2);color:#000}
.btn-ghost{background:transparent;border-color:rgba(255,255,255,.22);color:var(--text)}
.btn-ghost:hover{border-color:var(--lime);color:var(--lime)}
.when{
  display:grid;gap:6px;margin:0 0 18px;padding:12px 14px;border-radius:12px;
  border:1px solid rgba(255,255,255,.08);background:rgba(0,0,0,.35);
  font-size:13px;color:var(--muted);animation:rise .5s .1s ease both;
}
.when .lbl{
  display:inline-block;min-width:5.5rem;color:var(--lime);
  font:700 11px/1 var(--font-display);letter-spacing:.12em;text-transform:uppercase;
}
.when .hint{opacity:.75}
.main{animation:rise .55s .12s ease both}
.lead{color:var(--muted);margin:0 0 16px}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 16px}
.chip{
  display:inline-flex;align-items:center;gap:6px;padding:7px 12px;border-radius:999px;
  font:700 12px/1 var(--font-display);letter-spacing:.08em;text-transform:uppercase;
  color:#000;background:var(--lime);
}
.chip.soft{background:var(--lime-dim);color:var(--lime);border:1px solid var(--line)}
.panel{
  border:1px solid rgba(255,255,255,.08);border-radius:var(--radius);
  background:rgba(22,22,22,.92);margin:0 0 14px;overflow:hidden;
}
.panel-h{
  display:flex;align-items:center;justify-content:space-between;gap:10px;
  padding:14px 16px;border-bottom:1px solid rgba(255,255,255,.06);
  font:700 15px/1.2 var(--font-display);letter-spacing:.06em;text-transform:uppercase;
}
.panel-h .meta{color:var(--muted);font:600 12px/1 var(--font-body);letter-spacing:0;text-transform:none}
.game{
  display:grid;grid-template-columns:72px 1fr;gap:10px 14px;padding:14px 16px;
  border-top:1px solid rgba(255,255,255,.05);
}
.game:first-child{border-top:0}
.game .time{
  font:700 1.15rem/1 var(--font-display);color:var(--lime);letter-spacing:.04em;
}
.game .league{font-size:12px;color:var(--muted);margin:0 0 4px}
.game .matchup{font-weight:600}
.game .venue{font-size:13px;color:var(--muted);margin-top:4px}
.game .id{font-size:11px;color:#666}
.badge{
  display:inline-block;padding:3px 8px;border-radius:4px;font:700 11px/1 var(--font-display);
  letter-spacing:.08em;text-transform:uppercase;color:#000;background:var(--danger);margin-right:8px;
}
.badge.warn{background:var(--warn)}
.empty{
  padding:22px 18px;border-radius:var(--radius);border:1px dashed rgba(204,255,0,.35);
  background:rgba(204,255,0,.05);color:var(--text);
}
.empty strong{color:var(--lime)}
.change-list{margin:0;padding:0;list-style:none}
.change-list li{
  padding:12px 16px;border-top:1px solid rgba(255,255,255,.05);
  font-size:14px;line-height:1.45;color:var(--text);
}
.change-list li:first-child{border-top:0}
.delta{margin:4px 0 0;color:var(--muted);font-size:13px}
.delta code{
  font-family:ui-monospace,Consolas,monospace;font-size:12px;
  color:var(--text);background:rgba(0,0,0,.35);padding:1px 5px;border-radius:4px;
}
.delta .arrow{color:var(--lime);margin:0 4px}
details.band{
  margin:0 0 12px;border:1px solid rgba(255,255,255,.08);border-radius:var(--radius);
  background:rgba(22,22,22,.92);overflow:hidden;
}
details.band>summary{
  list-style:none;cursor:pointer;padding:14px 16px;display:flex;align-items:center;justify-content:space-between;
  font:700 15px/1 var(--font-display);letter-spacing:.08em;text-transform:uppercase;
  background:linear-gradient(90deg, rgba(204,255,0,.14), transparent 55%);
}
details.band>summary::-webkit-details-marker{display:none}
details.band>summary .chev{color:var(--lime);transition:transform .2s}
details.band[open]>summary .chev{transform:rotate(90deg)}
details.band .band-body{padding:8px 10px 12px}
.footer{
  margin-top:28px;padding-top:16px;border-top:1px solid rgba(255,255,255,.08);
  color:var(--muted);font-size:12px;line-height:1.55;
  display:flex;flex-direction:column;gap:8px;
}
.footer .footer-meta{
  display:flex;flex-wrap:wrap;gap:8px 16px;justify-content:space-between;align-items:baseline;
}
.footer .footer-credit,.footer .footer-powered{opacity:.9}
.footer a{color:var(--muted);text-decoration:underline;text-decoration-color:rgba(204,255,0,.28);text-underline-offset:2px}
.footer a:hover{color:var(--lime)}
.stats{
  display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin:0 0 18px;
}
.stat{
  padding:14px;border-radius:12px;border:1px solid rgba(255,255,255,.08);background:rgba(0,0,0,.28);
}
.stat b{display:block;font:700 1.5rem/1 var(--font-display);color:var(--lime);margin-top:6px}
.stat span{font:700 11px/1 var(--font-display);letter-spacing:.12em;text-transform:uppercase;color:var(--muted)}
@media (max-width:640px){
  .stats{grid-template-columns:1fr}
  .game{grid-template-columns:56px 1fr}
  .hero{padding:22px 18px}
  .brand img{width:44px;height:44px}
  .brand-name{font-size:1.25rem}
  .nav{
    width:100%;flex-wrap:nowrap;overflow-x:auto;-webkit-overflow-scrolling:touch;
    gap:6px;padding-bottom:2px;scrollbar-width:thin;
  }
  .nav a{flex:0 0 auto;padding:9px 12px;font-size:12px}
}
@keyframes rise{
  from{opacity:0;transform:translateY(10px)}
  to{opacity:1;transform:none}
}
@media (prefers-reduced-motion:reduce){
  *{animation:none!important;transition:none!important}
}
"""


def site_css() -> str:
    brand = brand_info()
    lime = brand.get("lime") or "#ccff00"
    ink = brand.get("ink") or "#0d0d0d"
    # Derive a slightly darker lime hover and dim overlay
    css = SITE_CSS
    css = css.replace("#ccff00", lime).replace("#0d0d0d", ink)
    return css


def csp_meta() -> str:
    if not active_cfg().get("fortress", {}).get("csp", True):
        return ""
    # Allow Google Fonts + club logo host + self. No inline scripts from third parties.
    club = club_info()
    logo = club.get("logoUrl") or ""
    extra_img = ""
    host = urlparse(logo).hostname if logo else None
    if host:
        extra_img = f" https://{host}"
    policy = (
        "default-src 'none'; "
        f"img-src 'self' data:{extra_img}; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com data:; "
        "script-src 'unsafe-inline'; "
        "connect-src 'none'; "
        "base-uri 'none'; "
        "form-action 'none'; "
        "frame-ancestors 'none'"
    )
    # Do not HTML-escape single quotes — CSP needs literal 'none' / 'self' / 'unsafe-inline'
    safe = policy.replace("&", "&amp;").replace('"', "&quot;")
    return f'<meta http-equiv="Content-Security-Policy" content="{safe}">'


def shell(
    title: str,
    body: str,
    *,
    last_run: datetime | None = None,
    next_run: datetime | None = None,
    active: str = "home",
    hero_title: str | None = None,
    hero_lead: str | None = None,
    hero_ctas: str = "",
) -> str:
    club = club_info()
    brand = brand_info()
    club_name = club.get("name") or "Club"
    club_url = club.get("url") or "#"
    logo_url = club.get("logoUrl") or ""
    ink = brand.get("ink") or "#0d0d0d"
    club_id = club.get("id") or ""
    when = schedule_banner(last_run, next_run, as_html=True)
    nav_items = [
        ("home", "index.html", "Home"),
        ("weekend", "weekend.html", "This week"),
        ("clashes", "clashes.html", "Clashes"),
        ("calendar", "calendar.html", "Calendar"),
        ("changes", "changes.html", "Changes"),
    ]
    nav = []
    for key, href, label in nav_items:
        cls = " is-active" if active == key else ""
        nav.append(f'<a class="{cls.strip()}" href="{href}">{label}</a>')
    hero = ""
    if hero_title:
        lead_html = f"<p>{escape(hero_lead)}</p>" if hero_lead else ""
        cta_html = f'<div class="cta-row">{hero_ctas}</div>' if hero_ctas else ""
        hero = (
            f'<section class="hero">'
            f"<h1>{escape(hero_title)}</h1>"
            f"{lead_html}{cta_html}"
            f"</section>"
        )
    logo_img = (
        f'<img src="{escape(logo_url, quote=True)}" alt="" width="56" height="56">'
        if logo_url
        else ""
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<meta name="theme-color" content="{escape(ink)}">
{csp_meta()}
<title>{escape(title)} · {escape(club_name)}</title>
{"<link rel=\"icon\" href=\"" + escape(logo_url, quote=True) + "\">" if logo_url else ""}
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Barlow:wght@400;600;700&family=Oswald:wght@500;700&display=swap" rel="stylesheet">
<style>{site_css()}</style>
</head>
<body>
<div class="shell">
  <header class="top">
    <a class="brand" href="index.html" aria-label="{escape(club_name)} schedule home">
      {logo_img}
      <div class="brand-text">
        <div class="brand-kicker">{escape(club_name)}</div>
        <div class="brand-name">Match Schedule</div>
      </div>
    </a>
    <nav class="nav" aria-label="Primary">{''.join(nav)}</nav>
  </header>
  {hero}
  {when}
  <main class="main">{body}</main>
  <footer class="footer">
    <div class="footer-meta">
      <div>Data from FloorballFlash · club {escape(str(club_id))}</div>
      <div><a href="{escape(club_url, quote=True)}" rel="noopener" target="_blank">{escape(urlparse(club_url).hostname or club_url)}</a></div>
    </div>
    <div class="footer-credit">Created by <a href="https://hamy.cz" target="_blank" rel="noopener">hamy.cz</a></div>
    <div class="footer-powered">Trainings powered by the <a href="https://interval-training-4bdbf.web.app/" target="_blank" rel="noopener">Interval Training</a> app · <a href="https://play.google.com/store/apps/details?id=com.intervaltraining.app&amp;utm_source=hamy.cz&amp;utm_medium=referral&amp;utm_campaign=interval-training" target="_blank" rel="noopener">Google Play</a></div>
  </footer>
</div>
</body>
</html>
"""


def resolve_timing(
    snapshot: dict[str, Any],
    last_success_path: Path | None = None,
    executed_at: datetime | None = None,
) -> tuple[datetime, datetime]:
    last = None
    if last_success_path and last_success_path.exists():
        try:
            ls = json.loads(last_success_path.read_text(encoding="utf-8"))
            last = parse_iso(ls.get("savedAt")) or parse_iso(ls.get("probedAt"))
        except Exception:
            pass
    if executed_at is not None:
        last = executed_at
    if last is None:
        last = parse_iso(snapshot.get("probedAt")) or datetime.now(timezone.utc)
    return last, next_cron_run(last)


def _game_block(
    g: dict[str, Any],
    *,
    show_date: bool = False,
    extra_html: str = "",
) -> str:
    cat = g.get("_category") or g["_band"]
    accent = CATEGORY_ACCENT.get(cat) or BAND_ACCENT.get(g["_band"], "#ccff00")
    t = escape(g.get("time") or "TBD")
    day = ""
    if show_date:
        day = f'<div style="font-size:11px;color:var(--muted);margin-bottom:2px">{escape(g.get("date") or "")}</div>'
    return (
        f'<article class="game">'
        f'<div class="time">{day}{t}</div>'
        f"<div>"
        f'<div class="league"><span style="color:{accent};font-weight:700">{escape(cat)}</span>'
        f' · {escape(g.get("competitionName") or "")}</div>'
        f'<div class="matchup">{escape(g.get("homeAway") or "")} vs '
        f'<strong>{escape(g.get("opponent") or "")}</strong></div>'
        f'<div class="venue">{escape(g.get("venue") or "")}'
        f' <span class="id">#{g["id"]}</span></div>'
        f"{extra_html}"
        f"</div></article>"
    )


def detect_clashes(games: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Same-squad same-day clashes + related multi-squad same-day awareness (e.g. all U12 groups).

    Calendars stay per category. Related groups add informational same-day overlaps
    when any two (or more) listed squads play.
    """
    by_date: dict[str, list] = defaultdict(list)
    for g in games:
        by_date[g["date"]].append(g)

    same: dict[str, list] = {c: [] for c in CATEGORY_ORDER}
    related: list[dict[str, Any]] = []

    for dt, gs in by_date.items():
        by_cat: dict[str, list] = defaultdict(list)
        for g in gs:
            by_cat[g["_category"]].append(g)

        # Same squad: 2+ competitions on one day
        for cat, cgs in by_cat.items():
            leagues = {g["competitionName"] for g in cgs}
            if len(cgs) >= 2 and len(leagues) > 1:
                same[cat].append(
                    {
                        "date": dt,
                        "games": sorted(cgs, key=lambda x: (x.get("time") or "99:99", x["id"])),
                        "leagues": sorted(leagues),
                    }
                )

        # Related groups: any 2+ of the listed squads on the same day
        for group in RELATED_SQUAD_GROUPS:
            present = [(c, by_cat[c]) for c in group["categories"] if by_cat.get(c)]
            if len(present) < 2:
                continue
            present_labels = [c for c, _ in present]
            related.append(
                {
                    "groupId": group["id"],
                    "label": group["label"],
                    "detail": group["detail"],
                    "kind": "sameDay",
                    "date": dt,
                    "squads": present_labels,
                    "games": sorted(
                        [g for _, pool in present for g in pool],
                        key=lambda x: (x.get("_category") or "", x.get("time") or "99:99", x["id"]),
                    ),
                }
            )

    return {"same": same, "related": related}


def _clash_day_details(items: list[dict[str, Any]], *, open_first: bool = True) -> list[str]:
    """Nested date accordion — summary is the date only."""
    parts: list[str] = []
    for di, item in enumerate(sorted(items, key=lambda x: x["date"])):
        open_attr = " open" if open_first and di == 0 else ""
        n = len(item.get("games") or [])
        parts.append(
            f'<details class="day-clash"{open_attr}>'
            f"<summary><span>{escape(fmt_date(item['date']))}</span>"
            f'<span class="meta">{n} · <span class="chev">▸</span></span></summary>'
            f'<div class="day-clash-body">'
        )
        for g in item["games"]:
            parts.append(_game_block(g))
        parts.append("</div></details>")
    return parts


def build_clashes(
    games: list[dict[str, Any]],
    last_run: datetime | None = None,
    next_run: datetime | None = None,
) -> str:
    """Same-squad same-day clashes + U12 group overlap awareness."""
    detected = detect_clashes(games)
    structure = detected["same"]
    related = detected["related"]

    active = [c for c in CATEGORY_ORDER if structure[c]]
    related_groups = {g["id"]: g for g in RELATED_SQUAD_GROUPS}
    related_active = sorted({r["groupId"] for r in related})

    if not active and not related_active:
        body = '<div class="empty"><strong>No clashes right now.</strong></div>'
        return shell(
            "Clashes",
            body,
            last_run=last_run,
            next_run=next_run,
            active="clashes",
            hero_title="Clashes",
        )

    parts: list[str] = []

    for gi, gid in enumerate(related_active):
        open_attr = " open" if gi == 0 else ""
        items = [r for r in related if r["groupId"] == gid]
        meta = related_groups[gid]
        parts.append(
            f'<details class="band"{open_attr} id="{gid}">'
            f'<summary><span style="color:#f5a524">{escape(meta["label"])}</span>'
            f'<span class="chev">▸</span></summary>'
            f'<div class="band-body">'
        )
        parts.extend(_clash_day_details(items, open_first=True))
        parts.append("</div></details>")

    for i, cat in enumerate(active):
        open_attr = " open" if i == 0 and not related_active else ""
        items = structure[cat]
        accent = CATEGORY_ACCENT.get(cat, "#ccff00")
        parts.append(
            f'<details class="band"{open_attr} id="{category_slug(cat)}">'
            f'<summary><span style="color:{accent}">{escape(cat)}</span>'
            f'<span class="chev">▸</span></summary>'
            f'<div class="band-body">'
        )
        parts.extend(_clash_day_details(items, open_first=True))
        parts.append("</div></details>")

    return shell(
        "Clashes",
        "\n".join(parts),
        last_run=last_run,
        next_run=next_run,
        active="clashes",
        hero_title="Clashes",
    )


def empty_changes_report(
    *,
    club_name: str | None = None,
    club_id: Any = None,
    old_probed_at: str | None = None,
    new_probed_at: str | None = None,
    compared_at: str | None = None,
) -> dict[str, Any]:
    """Site-safe empty change report (same shape as compare_snapshots.diff_snapshots)."""
    club = club_info()
    return {
        "comparedAt": compared_at
        or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "oldProbedAt": old_probed_at,
        "newProbedAt": new_probed_at,
        "clubId": club_id if club_id is not None else club.get("id"),
        "clubName": club_name or club.get("name") or "Club",
        "watchFields": list(
            active_cfg().get("schedule", {}).get("watchFields")
            or (
                "date",
                "time",
                "venue",
                "state",
                "homeTeam",
                "awayTeam",
                "competitionId",
                "competitionName",
            )
        ),
        "counts": {
            "added": 0,
            "removed": 0,
            "changed": 0,
            "cancellations": 0,
            "totalEvents": 0,
        },
        "added": [],
        "removed": [],
        "changed": [],
        "cancellations": [],
        "hasChanges": False,
    }


def load_changes_report(
    changes_path: Path | None,
    *,
    fallback_path: Path | None = None,
    snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Load changes JSON for the site page; fall back to durable copy or empty report."""
    for path in (changes_path, fallback_path):
        if path and path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and "counts" in data:
                    return data
            except Exception:
                pass
    probed = (snapshot or {}).get("probedAt")
    return empty_changes_report(
        club_name=(snapshot or {}).get("clubName"),
        club_id=(snapshot or {}).get("clubId"),
        old_probed_at=probed,
        new_probed_at=probed,
        compared_at=probed,
    )


def _change_opponent(game: dict[str, Any] | None) -> str:
    if not game:
        return ""
    if game.get("opponent"):
        return str(game["opponent"]).strip()
    ha = str(game.get("homeAway") or "").strip().lower()
    home, away = str(game.get("homeTeam") or "").strip(), str(game.get("awayTeam") or "").strip()
    if ha == "home":
        return away
    if ha == "away":
        return home
    return f"{home} vs {away}".strip()


def build_changes(
    report: dict[str, Any],
    last_run: datetime | None = None,
    next_run: datetime | None = None,
) -> str:
    """Public Pages page for the last successful watch diff (added / removed / field / cancel)."""
    counts = report.get("counts") or {}
    has = bool(report.get("hasChanges"))
    compared = report.get("comparedAt") or "—"
    old_p = report.get("oldProbedAt") or "—"
    new_p = report.get("newProbedAt") or "—"

    parts: list[str] = [
        '<p class="lead">Schedule updates from the last successful weekly watch '
        "(same diff as the email digest). Score-only updates are ignored.</p>",
        '<div class="when" role="status">',
        f'<div><span class="lbl">Compared</span> {escape(str(compared))}</div>',
        f'<div><span class="lbl">Previous</span> {escape(str(old_p))}</div>',
        f'<div><span class="lbl">New</span> {escape(str(new_p))}</div>',
        "</div>",
        '<div class="stats">',
        f'<div class="stat"><span>Changed</span><b>{int(counts.get("changed") or 0)}</b></div>',
        f'<div class="stat"><span>Added</span><b>{int(counts.get("added") or 0)}</b></div>',
        f'<div class="stat"><span>Removed</span><b>{int(counts.get("removed") or 0)}</b></div>',
        "</div>",
    ]
    cancel_n = int(counts.get("cancellations") or 0)
    if cancel_n:
        parts.append(
            f'<div class="chips"><span class="chip" style="background:var(--danger);color:#fff">'
            f"Cancellations · {cancel_n}</span></div>"
        )

    if not has:
        parts.append(
            '<div class="empty"><strong>No schedule changes since last check.</strong> '
            "This page reflects the last successful watch.</div>"
        )
        return shell(
            "Changes",
            "\n".join(parts),
            last_run=last_run,
            next_run=next_run,
            active="changes",
            hero_title="Changes",
            hero_lead="Recent schedule updates",
        )

    def section(title: str, items_html: str, count: int, *, danger: bool = False) -> str:
        accent = ' style="color:var(--danger)"' if danger else ""
        return (
            f'<section class="panel"><div class="panel-h"><span{accent}>{escape(title)}</span>'
            f'<span class="meta">{count} item(s)</span></div>'
            f'<ul class="change-list">{items_html}</ul></section>'
        )

    cancellations = report.get("cancellations") or []
    if cancellations:
        lis_parts = []
        for e in cancellations:
            summary = e.get("summary") or f"#{e.get('id')}"
            lis_parts.append(f"<li>{escape(str(summary))}</li>")
        parts.append(
            section(
                "Cancellations / state alerts",
                "".join(lis_parts),
                len(cancellations),
                danger=True,
            )
        )

    changed = report.get("changed") or []
    if changed:
        blocks: list[str] = []
        for e in changed:
            after = e.get("after") or {}
            gid = e.get("id")
            league = escape(str(after.get("competitionName") or ""))
            opp = escape(_change_opponent(after))
            deltas = []
            for c in e.get("changes") or []:
                deltas.append(
                    f'<div class="delta"><strong>{escape(str(c.get("field")))}</strong>: '
                    f'<code>{escape(str(c.get("from")))}</code>'
                    f'<span class="arrow">→</span>'
                    f'<code>{escape(str(c.get("to")))}</code></div>'
                )
            blocks.append(
                f"<li><strong>#{escape(str(gid))}</strong> · {league}<br>"
                f'<span style="color:var(--muted)">vs {opp}</span>'
                f"{''.join(deltas)}</li>"
            )
        parts.append(
            f'<section class="panel"><div class="panel-h"><span>Field changes</span>'
            f'<span class="meta">{len(changed)} game(s)</span></div>'
            f'<ul class="change-list">{"".join(blocks)}</ul></section>'
        )

    added = report.get("added") or []
    if added:
        lis_parts = []
        for e in added:
            summary = e.get("summary") or f"New game #{e.get('id')}"
            lis_parts.append(f"<li>{escape(str(summary))}</li>")
        parts.append(section("New games", "".join(lis_parts), len(added)))

    removed = report.get("removed") or []
    if removed:
        lis_parts = []
        for e in removed:
            summary = e.get("summary") or f"Removed game #{e.get('id')}"
            lis_parts.append(f"<li>{escape(str(summary))}</li>")
        parts.append(section("Removed games", "".join(lis_parts), len(removed)))

    fields = report.get("watchFields") or []
    if fields:
        parts.append(
            f'<p class="lead" style="margin-top:8px">Watched fields: '
            f'{escape(", ".join(str(f) for f in fields))}.</p>'
        )

    return shell(
        "Changes",
        "\n".join(parts),
        last_run=last_run,
        next_run=next_run,
        active="changes",
        hero_title="Changes",
        hero_lead="Recent schedule updates",
    )


def build_weekend(
    games: list[dict[str, Any]],
    today: date | None = None,
    pages_base_url: str = "",
    last_run: datetime | None = None,
    next_run: datetime | None = None,
) -> str:
    """This calendar week (Mon–Sun) — filename stays weekend.html for stable URLs."""
    del pages_base_url
    mon, sun = current_week(today)
    mon_s, sun_s = mon.isoformat(), sun.isoformat()
    week_dates = {(mon + timedelta(days=i)).isoformat() for i in range(7)}
    week_games = [g for g in games if g.get("date") in week_dates]
    week_games.sort(key=lambda g: (g.get("date") or "", g.get("time") or "99:99", g["id"]))

    by_cat: dict[str, list] = defaultdict(list)
    for g in week_games:
        by_cat[g["_category"]].append(g)

    chips = []
    for c in CATEGORY_ORDER:
        if c in by_cat:
            accent = CATEGORY_ACCENT.get(c, "#ccff00")
            chips.append(
                f'<span class="chip" style="background:{accent}">{escape(c)} · {len(by_cat[c])}</span>'
            )

    parts = [
        f'<div class="chips">{"".join(chips)}</div>' if chips else "",
    ]
    if not week_games:
        parts.append(
            f'<div class="empty"><strong>No {escape(club_info().get("name") or "club")} games</strong> '
            f"this week.</div>"
        )
    else:
        for i in range(7):
            day = (mon + timedelta(days=i)).isoformat()
            day_games = [g for g in week_games if g["date"] == day]
            if not day_games:
                continue
            parts.append(
                f'<section class="panel"><div class="panel-h">'
                f'<span>{escape(fmt_date(day))}</span>'
                f'<span class="meta">{len(day_games)} game(s)</span></div>'
            )
            for g in day_games:
                parts.append(_game_block(g))
            parts.append("</section>")

    return shell(
        "This week",
        "\n".join(p for p in parts if p),
        last_run=last_run,
        next_run=next_run,
        active="weekend",
        hero_title="This week",
        hero_lead=f"{fmt_date(mon_s)} – {fmt_date(sun_s)}",
    )


def build_index(
    snapshot: dict[str, Any],
    pages_base_url: str = "",
    last_run: datetime | None = None,
    next_run: datetime | None = None,
    weekend_n: int = 0,
    clash_bands: list[str] | None = None,
    games: list[dict[str, Any]] | None = None,
    today: date | None = None,
) -> str:
    del games, today  # kept for call-site compatibility
    n = len(snapshot.get("games") or [])
    bands = clash_bands or []
    bands_stat = escape(", ".join(bands)) if bands else "None"
    body = f"""
<div class="stats">
  <div class="stat"><span>Games in baseline</span><b>{n}</b></div>
  <div class="stat"><span>This week</span><b>{weekend_n}</b></div>
  <div class="stat"><span>Clashes</span><b style="font-size:1.15rem">{bands_stat}</b></div>
</div>
"""
    club = club_info()
    return shell(
        "Schedule",
        body,
        last_run=last_run,
        next_run=next_run,
        active="home",
        hero_title="Schedule",
        hero_lead=(
            f"Club {club.get('id')} · {club.get('name') or 'Club'} · "
            f"{n} league games in the current baseline."
        ),
    )


def _clash_categories(games: list[dict[str, Any]]) -> list[str]:
    """Labels for home-page clash chips: same-squad categories + related U12 groups."""
    detected = detect_clashes(games)
    labels: list[str] = []
    related_ids = {r["groupId"] for r in detected["related"]}
    for group in RELATED_SQUAD_GROUPS:
        if group["id"] in related_ids:
            labels.append(group["label"])
    for cat in CATEGORY_ORDER:
        if detected["same"][cat]:
            labels.append(cat)
    return labels


def _ics_escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def _ics_dtstamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _ics_fold(content: str) -> str:
    """RFC 5545: CRLF endings + fold lines longer than 75 octets."""
    out: list[str] = []
    for raw in content.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        data = raw.encode("utf-8")
        if not data:
            out.append("")
            continue
        first = True
        while data:
            limit = 75 if first else 74  # continuation lines start with a space
            if len(data) <= limit:
                chunk, data = data, b""
            else:
                cut = limit
                while cut > 0 and (data[cut] & 0xC0) == 0x80:
                    cut -= 1
                if cut == 0:
                    cut = limit
                chunk, data = data[:cut], data[cut:]
            text = chunk.decode("utf-8")
            out.append(text if first else f" {text}")
            first = False
    return "\r\n".join(out) + "\r\n"


ICS_VERIFY_NOTE = (
    "Please double-check the date and kickoff time — the schedule can change."
)


def _game_line(g: dict[str, Any]) -> str:
    tm = g.get("time") or "TBD"
    matchup = f"{g.get('homeAway') or ''} vs {g.get('opponent') or '?'}".strip()
    venue = g.get("venue") or "TBD"
    return f"{tm} · {matchup} · {venue} · #{g['id']}"


def _vevent(g: dict[str, Any], category: str) -> str:
    """Single-game timed (or all-day) event — used for one-game downloads and solo days."""
    gid = g["id"]
    ds = g.get("date") or ""
    tm = g.get("time")
    mins = parse_time(tm)
    tzid = schedule_timezone()
    stamp = _ics_dtstamp()
    game_day = date.fromisoformat(ds)
    if mins is not None:
        start = datetime.strptime(f"{ds} {tm}", "%Y-%m-%d %H:%M")
        end = start + timedelta(minutes=90)
        dtstart = start.strftime("%Y%m%dT%H%M%S")
        dtend = end.strftime("%Y%m%dT%H%M%S")
        time_lines = f"DTSTART;TZID={tzid}:{dtstart}\nDTEND;TZID={tzid}:{dtend}"
    else:
        # All-day: DTEND is exclusive (next calendar day)
        end_day = game_day + timedelta(days=1)
        time_lines = (
            f"DTSTART;VALUE=DATE:{game_day.strftime('%Y%m%d')}\n"
            f"DTEND;VALUE=DATE:{end_day.strftime('%Y%m%d')}"
        )

    summary = f"{category}: vs {g.get('opponent') or '?'}"
    desc_parts = [
        ICS_VERIFY_NOTE,
        g.get("competitionName") or "",
        f"{g.get('homeAway') or ''} vs {g.get('opponent') or ''}".strip(),
        f"Time: {tm or 'TBD'} ({tzid})",
        f"Venue: {g.get('venue') or 'TBD'}",
        f"Game #{gid}",
    ]
    desc = _ics_escape("\n".join(p for p in desc_parts if p))
    loc = _ics_escape(g.get("venue") or "")
    # No VALARM — Google/Apple ignore ICS alarms and inject account defaults (often Email/30min).
    return (
        "BEGIN:VEVENT\n"
        f"UID:{ics_uid('game', gid)}\n"
        f"DTSTAMP:{stamp}\n"
        f"{time_lines}\n"
        f"SUMMARY:{_ics_escape(summary)}\n"
        f"DESCRIPTION:{desc}\n"
        f"LOCATION:{loc}\n"
        "END:VEVENT"
    )


def _day_vevent(games: list[dict[str, Any]], category: str) -> str:
    """One calendar entry for a day with multiple games (list in description)."""
    ds = games[0].get("date") or ""
    tzid = schedule_timezone()
    stamp = _ics_dtstamp()
    game_day = date.fromisoformat(ds)
    timed = [(g, parse_time(g.get("time"))) for g in games]
    if all(m is not None for _, m in timed):
        starts = [
            datetime.strptime(f"{ds} {g.get('time')}", "%Y-%m-%d %H:%M") for g, _ in timed
        ]
        start = min(starts)
        end = max(starts) + timedelta(minutes=90)
        time_lines = (
            f"DTSTART;TZID={tzid}:{start.strftime('%Y%m%dT%H%M%S')}\n"
            f"DTEND;TZID={tzid}:{end.strftime('%Y%m%dT%H%M%S')}"
        )
    else:
        end_day = game_day + timedelta(days=1)
        time_lines = (
            f"DTSTART;VALUE=DATE:{game_day.strftime('%Y%m%d')}\n"
            f"DTEND;VALUE=DATE:{end_day.strftime('%Y%m%d')}"
        )
    n = len(games)
    summary = f"{category}: {n} games"
    desc_parts = [
        ICS_VERIFY_NOTE,
        f"{fmt_date(ds)} · {n} games · {tzid}",
        "",
        *[_game_line(g) for g in games],
    ]
    desc = _ics_escape("\n".join(desc_parts))
    venues = [g.get("venue") or "" for g in games if g.get("venue")]
    loc = venues[0] if len(set(venues)) == 1 and venues else ""
    return (
        "BEGIN:VEVENT\n"
        f"UID:{ics_uid('day', ds)}\n"
        f"DTSTAMP:{stamp}\n"
        f"{time_lines}\n"
        f"SUMMARY:{_ics_escape(summary)}\n"
        f"DESCRIPTION:{desc}\n"
        f"LOCATION:{_ics_escape(loc)}\n"
        "END:VEVENT"
    )


def build_ics(games: list[dict[str, Any]], category: str) -> str:
    """Season / day ICS: one VEVENT per game day (no alarms)."""
    club_name = club_info().get("name") or "Club"
    tzid = schedule_timezone()
    by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for g in games:
        ds = g.get("date") or ""
        if ds:
            by_date[ds].append(g)
    events: list[str] = []
    for ds in sorted(by_date.keys()):
        day_games = sorted(
            by_date[ds],
            key=lambda x: (x.get("time") or "99:99", x["id"]),
        )
        if len(day_games) == 1:
            events.append(_vevent(day_games[0], category))
        else:
            events.append(_day_vevent(day_games, category))
    cal_name = f"{club_name} {category}"
    raw = (
        "BEGIN:VCALENDAR\n"
        "VERSION:2.0\n"
        f"PRODID:-//{club_name}//Schedule//EN\n"
        "CALSCALE:GREGORIAN\n"
        "METHOD:PUBLISH\n"
        f"X-WR-CALNAME:{cal_name}\n"
        f"X-WR-TIMEZONE:{tzid}\n"
        f"{VIENNA_VTIMEZONE}\n"
        + "\n".join(events)
        + "\nEND:VCALENDAR\n"
    )
    return _ics_fold(raw)


def build_ics_single(g: dict[str, Any], category: str) -> str:
    """One-game download: single timed event (no alarms)."""
    club_name = club_info().get("name") or "Club"
    tzid = schedule_timezone()
    events = [_vevent(g, category)]
    cal_name = f"{club_name} {category}"
    raw = (
        "BEGIN:VCALENDAR\n"
        "VERSION:2.0\n"
        f"PRODID:-//{club_name}//Schedule//EN\n"
        "CALSCALE:GREGORIAN\n"
        "METHOD:PUBLISH\n"
        f"X-WR-CALNAME:{cal_name}\n"
        f"X-WR-TIMEZONE:{tzid}\n"
        f"{VIENNA_VTIMEZONE}\n"
        + "\n".join(events)
        + "\nEND:VCALENDAR\n"
    )
    return _ics_fold(raw)


def write_category_ics(games: list[dict[str, Any]], ics_dir: Path) -> list[dict[str, Any]]:
    ics_dir.mkdir(parents=True, exist_ok=True)
    # clear old ics
    for old in ics_dir.glob("*.ics"):
        old.unlink()
    by_cat: dict[str, list] = defaultdict(list)
    for g in games:
        by_cat[g["_category"]].append(g)
    index = []
    for cat in CATEGORY_ORDER:
        cgs = sorted(by_cat.get(cat) or [], key=lambda x: (x.get("date") or "", x.get("time") or "99:99", x["id"]))
        if not cgs:
            continue
        slug = category_slug(cat)
        path = ics_dir / f"{slug}.ics"
        # Binary write preserves CRLF produced by _ics_fold
        path.write_bytes(build_ics(cgs, cat).encode("utf-8"))
        dates = sorted({g["date"] for g in cgs})
        index.append({"category": cat, "slug": slug, "file": f"ics/{slug}.ics", "games": len(cgs), "dates": dates})
    return index


def build_calendar(
    games: list[dict[str, Any]],
    ics_index: list[dict[str, Any]],
    last_run: datetime | None = None,
    next_run: datetime | None = None,
    today: date | None = None,
) -> str:
    payload = []
    for g in games:
        payload.append(
            {
                "id": g["id"],
                "date": g.get("date"),
                "time": g.get("time"),
                "category": g["_category"],
                "competitionName": g.get("competitionName"),
                "homeAway": g.get("homeAway"),
                "opponent": g.get("opponent"),
                "venue": g.get("venue"),
            }
        )
    data_json = json.dumps(payload, ensure_ascii=False)
    cats_json = json.dumps(
        [{"category": x["category"], "slug": x["slug"], "file": x["file"], "games": x["games"]} for x in ics_index],
        ensure_ascii=False,
    )
    today_s = (today or date.today()).isoformat()
    club = club_info()
    club_name_js = json.dumps(club.get("name") or "Club")
    club_slug_js = json.dumps(club.get("slug") or "club")
    tzid_js = json.dumps(schedule_timezone())
    # Escape VTIMEZONE for embedding inside a JS string literal via JSON
    vtz_js = json.dumps(VIENNA_VTIMEZONE)

    extra_css = """
.cal-wrap{display:grid;gap:14px;min-width:0}
.cal-legend{
  display:flex;flex-wrap:wrap;gap:10px 16px;align-items:center;
  font:600 12px/1.3 var(--font-body);color:var(--muted);
}
.cal-legend .swatch{
  display:inline-flex;align-items:center;justify-content:center;
  width:22px;height:22px;border-radius:7px;margin-right:6px;vertical-align:middle;
  font:700 11px/1 var(--font-display);color:#000;background:var(--lime);
  box-shadow:0 0 0 1px rgba(204,255,0,.5);
}
.cal-legend .swatch-today{
  background:transparent;color:var(--text);
  box-shadow:inset 0 0 0 2px rgba(255,255,255,.55);
}
.cal-empty{
  margin:0;padding:10px 12px;border-radius:10px;
  border:1px dashed rgba(255,255,255,.18);background:rgba(0,0,0,.22);
  color:var(--muted);font:600 13px/1.4 var(--font-body);
}
.cal-empty[hidden]{display:none}
.cat-select-wrap{display:none;margin:0 0 4px}
.cat-select-wrap label{
  display:block;font:700 11px/1 var(--font-display);letter-spacing:.1em;text-transform:uppercase;
  color:var(--muted);margin:0 0 8px;
}
.cat-select{
  width:100%;appearance:none;-webkit-appearance:none;
  border:1px solid var(--lime);border-radius:12px;padding:14px 44px 14px 16px;
  min-height:52px;font:700 16px/1.2 var(--font-display);letter-spacing:.04em;text-transform:uppercase;
  color:#000;background:var(--lime)
    url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='20' height='20' viewBox='0 0 24 24' fill='none' stroke='%23000' stroke-width='2.5'%3E%3Cpath d='M6 9l6 6 6-6'/%3E%3C/svg%3E")
    no-repeat right 14px center;
  box-shadow:0 0 0 3px rgba(204,255,0,.18);
}
.cat-select:focus{outline:2px solid #fff;outline-offset:2px}
.cat-bar{display:flex;flex-wrap:wrap;gap:8px}
.cat-bar button{
  border:1px solid var(--line);background:rgba(255,255,255,.03);color:var(--text);
  border-radius:999px;padding:10px 14px;cursor:pointer;min-height:40px;
  font:700 12px/1 var(--font-display);letter-spacing:.06em;text-transform:uppercase;
}
.cat-bar button.is-on{background:var(--lime);color:#000;border-color:var(--lime)}
.cal-toolbar{display:flex;flex-wrap:wrap;gap:10px;align-items:center;justify-content:space-between}
.cal-toolbar .nav-row{display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.cal-grid{
  display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:8px;
  min-width:0;
}
.cal-dow{text-align:center;font:700 11px/1 var(--font-display);letter-spacing:.08em;color:var(--muted);padding:6px 0}
.cal-cell{
  min-height:110px;border:1px solid rgba(255,255,255,.08);border-radius:10px;
  background:rgba(0,0,0,.28);padding:6px;cursor:default;position:relative;
  display:flex;flex-direction:column;gap:4px;min-width:0;overflow:hidden;
}
button.cal-cell{
  font:inherit;color:inherit;text-align:left;width:100%;
  appearance:none;-webkit-appearance:none;
}
.cal-cell.has{
  cursor:pointer;border-color:rgba(204,255,0,.75);background:rgba(204,255,0,.16);
  box-shadow:inset 0 0 0 1px rgba(204,255,0,.35);
}
.cal-cell.has:hover,.cal-cell.is-sel{background:rgba(204,255,0,.26);border-color:var(--lime)}
.cal-cell.is-today{box-shadow:inset 0 0 0 2px rgba(255,255,255,.45)}
.cal-cell.has.is-today{box-shadow:inset 0 0 0 2px rgba(255,255,255,.55), inset 0 0 0 1px rgba(204,255,0,.35)}
.cal-cell .d{font:700 13px/1 var(--font-display);color:var(--muted)}
.cal-cell.has .d{
  display:inline-flex;align-items:center;justify-content:center;
  min-width:1.55em;height:1.55em;padding:0 4px;border-radius:999px;
  background:var(--lime);color:#000;
}
.cal-cell .cnt{display:none}
.cal-cell .glist{display:flex;flex-direction:column;gap:3px;margin-top:2px;min-width:0}
.cal-cell .g{
  font:600 11px/1.25 var(--font-body);color:var(--text);
  background:rgba(0,0,0,.45);border-left:3px solid var(--lime);
  padding:3px 5px;border-radius:4px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
}
.cal-cell .g .t{color:var(--lime);font-weight:700;margin-right:4px}
.cal-cell.mute{opacity:.28;min-height:48px}
.day-panel{margin-top:8px}
.day-panel[hidden]{display:none}
.dl-row{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0 12px}
@media (max-width:800px){
  .cat-select-wrap{display:block}
  .cat-bar{display:none}
  .cal-toolbar{flex-direction:column;align-items:stretch;gap:10px}
  .cal-toolbar .nav-row{justify-content:center}
  .cal-toolbar .btn{min-height:44px;padding:12px 16px}
  .cal-toolbar #monthLabel{font-size:1rem;margin:0 8px !important}
  .cal-grid{gap:4px}
  .cal-dow{font-size:10px;letter-spacing:.04em;padding:2px 0}
  .cal-cell{
    min-height:52px;padding:4px 2px 6px;border-radius:8px;
    align-items:center;justify-content:flex-start;gap:3px;
  }
  .cal-cell.has{
    background:rgba(204,255,0,.28);border-color:var(--lime);
    border-width:2px;
  }
  .cal-cell.mute{min-height:52px}
  .cal-cell .d{font-size:12px}
  .cal-cell.has .d{
    min-width:1.7em;height:1.7em;font-size:12px;
  }
  .cal-cell .glist{display:none}
  .cal-cell.has .cnt{
    display:inline-flex;align-items:center;justify-content:center;
    min-width:1.35em;padding:2px 5px;border-radius:999px;
    font:700 10px/1 var(--font-display);letter-spacing:.02em;
    color:#000;background:var(--lime);
  }
  .day-panel .game{grid-template-columns:52px 1fr}
  .dl-row .btn{width:100%;min-height:44px}
}
"""

    body = f"""
<style>{extra_css}</style>
<p class="lead">Pick a squad. Lime days have games — tap a day for details and downloads.</p>
<div class="cal-wrap">
  <div class="cal-legend" aria-hidden="true">
    <span><span class="swatch">12</span> Game day</span>
    <span><span class="swatch swatch-today">12</span> Today</span>
  </div>
  <div class="cat-select-wrap">
    <label for="catSelect">Squad</label>
    <select id="catSelect" class="cat-select" aria-label="Squad category"></select>
  </div>
  <div class="cat-bar" id="catBar" role="tablist" aria-label="Squad categories"></div>
  <div class="cal-toolbar">
    <div class="nav-row">
      <button type="button" class="btn btn-ghost" id="prevMonth" aria-label="Previous month">←</button>
      <strong id="monthLabel" style="margin:0 10px;font:700 1.1rem var(--font-display);letter-spacing:.06em;text-transform:uppercase"></strong>
      <button type="button" class="btn btn-ghost" id="nextMonth" aria-label="Next month">→</button>
      <button type="button" class="btn btn-ghost" id="thisMonth">Today</button>
    </div>
    <div class="nav-row">
      <a class="btn btn-lime" id="dlAll" href="#">Download season (.ics)</a>
    </div>
  </div>
  <p class="cal-empty" id="calEmpty" hidden></p>
  <div class="cal-grid" id="calDows"></div>
  <div class="cal-grid" id="calGrid"></div>
  <section class="panel day-panel" id="dayPanel" hidden>
    <div class="panel-h"><span id="dayTitle">Day</span><span class="meta" id="dayMeta"></span></div>
    <div class="dl-row">
      <button type="button" class="btn btn-lime" id="dlDay">Download this day (.ics)</button>
    </div>
    <div id="dayGames"></div>
  </section>
</div>
<script>
const GAMES = {data_json};
const CATS = {cats_json};
const TODAY = "{today_s}";
const CLUB_NAME = {club_name_js};
const CLUB_SLUG = {club_slug_js};
const TZID = {tzid_js};
const VTIMEZONE = {vtz_js};
const DOW = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"];
function pickDefaultCat(){{
  if (!CATS.length) return null;
  const monthPrefix = TODAY.slice(0, 7); // YYYY-MM
  const withMonth = CATS.find(c => GAMES.some(g => g.category === c.category && (g.date||"").startsWith(monthPrefix)));
  if (withMonth) return withMonth.category;
  const upcoming = CATS.map(c => {{
    const next = GAMES.filter(g => g.category === c.category && g.date && g.date >= TODAY)
      .map(g => g.date).sort()[0] || null;
    return {{ category: c.category, next }};
  }}).filter(x => x.next).sort((a,b) => a.next.localeCompare(b.next));
  return (upcoming[0] && upcoming[0].category) || CATS[0].category;
}}
function catFromQuery(){{
  const raw = new URLSearchParams(location.search).get("cat");
  if (!raw || !CATS.length) return null;
  const key = String(raw).trim().toLowerCase();
  const hit = CATS.find(c =>
    String(c.slug||"").toLowerCase() === key ||
    String(c.category||"").toLowerCase() === key
  );
  return hit ? hit.category : null;
}}
function syncCatQuery(){{
  const meta = CATS.find(c => c.category === cat);
  const slug = meta && meta.slug ? meta.slug : "";
  const url = new URL(location.href);
  if (slug) url.searchParams.set("cat", slug);
  else url.searchParams.delete("cat");
  history.replaceState(null, "", url.pathname + url.search + url.hash);
}}
function monthForCat(catName){{
  const upcoming = gamesFor(catName).map(g => g.date).filter(Boolean).sort().find(d => d >= TODAY);
  const anchorDate = upcoming || TODAY;
  const parts = anchorDate.split("-").map(Number);
  return new Date(parts[0], parts[1]-1, 1);
}}
let cat = catFromQuery() || pickDefaultCat();
let anchor = new Date(TODAY + "T12:00:00");
let view = monthForCat(cat);
let selectedDate = null;

function pad(n){{return String(n).padStart(2,"0");}}
function ymd(d){{return d.getFullYear()+"-"+pad(d.getMonth()+1)+"-"+pad(d.getDate());}}
function escapeHtml(s){{
  return String(s??"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;").replace(/'/g,"&#39;");
}}
function shortOpp(s){{
  s = String(s||"?");
  return s.length > 18 ? s.slice(0,16)+"…" : s;
}}
function gamesFor(catName, dateStr){{
  return GAMES.filter(g => g.category === catName && (!dateStr || g.date === dateStr))
    .sort((a,b)=> (a.date||"").localeCompare(b.date||"") || (a.time||"99:99").localeCompare(b.time||"99:99"));
}}
function icsEscape(s){{
  return String(s||"").replace(/\\\\/g,"\\\\\\\\").replace(/;/g,"\\\\;").replace(/,/g,"\\\\,").replace(/\\n/g,"\\\\n");
}}
function icsUtcStamp(){{
  const d = new Date();
  return d.getUTCFullYear()+pad(d.getUTCMonth()+1)+pad(d.getUTCDate())+"T"+
    pad(d.getUTCHours())+pad(d.getUTCMinutes())+pad(d.getUTCSeconds())+"Z";
}}
function icsFold(text){{
  // RFC 5545: CRLF + fold long lines (~75 chars)
  const lines = String(text).replace(/\\r\\n/g,"\\n").replace(/\\r/g,"\\n").split("\\n");
  const out = [];
  lines.forEach(line => {{
    if (!line) {{ out.push(""); return; }}
    let s = line;
    let first = true;
    while (s.length > 75) {{
      out.push(s.slice(0, 75));
      s = " " + s.slice(75);
      first = false;
    }}
    out.push(first ? s : s);
  }});
  return out.join("\\r\\n") + "\\r\\n";
}}
function fmtLocal(d){{
  return d.getFullYear()+pad(d.getMonth()+1)+pad(d.getDate())+"T"+
    pad(d.getHours())+pad(d.getMinutes())+pad(d.getSeconds());
}}
function icsUid(){{
  const parts = Array.prototype.slice.call(arguments).filter(p => p != null && String(p) !== "");
  return "urn:"+CLUB_SLUG+":"+parts.join(":");
}}
const ICS_VERIFY_NOTE = "Please double-check the date and kickoff time — the schedule can change.";
function gameLine(g){{
  return (g.time||"TBD")+" · "+((g.homeAway||"")+" vs "+(g.opponent||"?")).trim()+" · "+(g.venue||"TBD")+" · #"+g.id;
}}
function vevent(g){{
  let timeLines;
  const stamp = icsUtcStamp();
  const parts = g.date.split("-").map(Number);
  if (g.time) {{
    const [hh, mm] = String(g.time).split(":").map(Number);
    const startLocal = new Date(parts[0], parts[1]-1, parts[2], hh, mm, 0);
    const endLocal = new Date(startLocal.getTime() + 90*60000);
    timeLines = "DTSTART;TZID="+TZID+":"+fmtLocal(startLocal)+"\\nDTEND;TZID="+TZID+":"+fmtLocal(endLocal);
  }} else {{
    const startLocal = new Date(parts[0], parts[1]-1, parts[2], 0, 0, 0);
    const endLocal = new Date(parts[0], parts[1]-1, parts[2]+1);
    const ymdOf = d => d.getFullYear()+pad(d.getMonth()+1)+pad(d.getDate());
    timeLines = "DTSTART;VALUE=DATE:"+ymdOf(startLocal)+"\\nDTEND;VALUE=DATE:"+ymdOf(endLocal);
  }}
  const summary = icsEscape(g.category+": vs "+(g.opponent||"?"));
  const desc = icsEscape([ICS_VERIFY_NOTE, g.competitionName, (g.homeAway||"")+" vs "+(g.opponent||""), "Time: "+(g.time||"TBD")+" ("+TZID+")", "Venue: "+(g.venue||"TBD"), "Game #"+g.id].filter(Boolean).join("\\n"));
  return "BEGIN:VEVENT\\nUID:"+icsUid("game", g.id)+"\\nDTSTAMP:"+stamp+"\\n"+timeLines+"\\nSUMMARY:"+summary+"\\nDESCRIPTION:"+desc+"\\nLOCATION:"+icsEscape(g.venue||"")+"\\nEND:VEVENT";
}}
function veventDay(list, catName){{
  const ds = list[0].date;
  const stamp = icsUtcStamp();
  let timeLines;
  const parts = ds.split("-").map(Number);
  if (list.every(g => g.time)) {{
    const startsLocal = list.map(g => {{
      const [hh, mm] = String(g.time).split(":").map(Number);
      const p = g.date.split("-").map(Number);
      return new Date(p[0], p[1]-1, p[2], hh, mm, 0);
    }});
    const startLocal = new Date(Math.min.apply(null, startsLocal));
    const endLocal = new Date(Math.max.apply(null, startsLocal) + 90*60000);
    timeLines = "DTSTART;TZID="+TZID+":"+fmtLocal(startLocal)+"\\nDTEND;TZID="+TZID+":"+fmtLocal(endLocal);
  }} else {{
    const startLocal = new Date(parts[0], parts[1]-1, parts[2], 0, 0, 0);
    const endLocal = new Date(parts[0], parts[1]-1, parts[2]+1);
    const ymdOf = d => d.getFullYear()+pad(d.getMonth()+1)+pad(d.getDate());
    timeLines = "DTSTART;VALUE=DATE:"+ymdOf(startLocal)+"\\nDTEND;VALUE=DATE:"+ymdOf(endLocal);
  }}
  const n = list.length;
  const summary = icsEscape(catName+": "+n+" games");
  const desc = icsEscape([ICS_VERIFY_NOTE, ds+" · "+n+" games · "+TZID, ""].concat(list.map(gameLine)).join("\\n"));
  const venues = list.map(g => g.venue||"").filter(Boolean);
  const loc = (venues.length && venues.every(v => v === venues[0])) ? venues[0] : "";
  return "BEGIN:VEVENT\\nUID:"+icsUid("day", ds)+"\\nDTSTAMP:"+stamp+"\\n"+timeLines+"\\nSUMMARY:"+summary+"\\nDESCRIPTION:"+desc+"\\nLOCATION:"+icsEscape(loc)+"\\nEND:VEVENT";
}}
function icsFile(list, catName){{
  // Day / season: one VEVENT per game day (no alarms).
  const byDate = {{}};
  list.forEach(g => {{
    if (!g.date) return;
    if (!byDate[g.date]) byDate[g.date] = [];
    byDate[g.date].push(g);
  }});
  const events = [];
  Object.keys(byDate).sort().forEach(ds => {{
    const dayGames = byDate[ds].slice().sort((a,b)=>(a.time||"99:99").localeCompare(b.time||"99:99")||String(a.id).localeCompare(String(b.id)));
    if (dayGames.length === 1) events.push(vevent(Object.assign({{category: catName}}, dayGames[0])));
    else events.push(veventDay(dayGames.map(g => Object.assign({{category: catName}}, g)), catName));
  }});
  const name = CLUB_NAME+" "+catName;
  const raw = "BEGIN:VCALENDAR\\nVERSION:2.0\\nPRODID:-//"+CLUB_NAME+"//Schedule//EN\\nCALSCALE:GREGORIAN\\nMETHOD:PUBLISH\\nX-WR-CALNAME:"+name+"\\nX-WR-TIMEZONE:"+TZID+"\\n"+VTIMEZONE+"\\n"+events.join("\\n")+"\\nEND:VCALENDAR\\n";
  return icsFold(raw);
}}
function icsFileSingle(g, catName){{
  // Individual game download — one timed event (no alarms).
  const game = Object.assign({{category: catName}}, g);
  const events = [vevent(game)];
  const name = CLUB_NAME+" "+catName;
  const raw = "BEGIN:VCALENDAR\\nVERSION:2.0\\nPRODID:-//"+CLUB_NAME+"//Schedule//EN\\nCALSCALE:GREGORIAN\\nMETHOD:PUBLISH\\nX-WR-CALNAME:"+name+"\\nX-WR-TIMEZONE:"+TZID+"\\n"+VTIMEZONE+"\\n"+events.join("\\n")+"\\nEND:VCALENDAR\\n";
  return icsFold(raw);
}}
function downloadIcs(filename, text){{
  const blob = new Blob([text], {{type:"text/calendar;charset=utf-8"}});
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = filename; a.click();
  URL.revokeObjectURL(url);
}}
function slugFor(catName){{
  return (CATS.find(c=>c.category===catName)||{{}}).slug || "category";
}}
function setCategory(nextCat){{
  if (!nextCat || nextCat === cat) return;
  cat = nextCat;
  selectedDate = null;
  view = monthForCat(cat);
  syncCatQuery();
  renderAll();
}}
function renderCats(){{
  const bar = document.getElementById("catBar");
  bar.innerHTML = "";
  CATS.forEach(c => {{
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = c.category + " · " + c.games;
    if (c.category === cat) b.classList.add("is-on");
    b.onclick = () => setCategory(c.category);
    bar.appendChild(b);
  }});
  const sel = document.getElementById("catSelect");
  if (sel) {{
    sel.innerHTML = "";
    CATS.forEach(c => {{
      const opt = document.createElement("option");
      opt.value = c.category;
      opt.textContent = c.category + " · " + c.games;
      if (c.category === cat) opt.selected = true;
      sel.appendChild(opt);
    }});
    sel.onchange = () => setCategory(sel.value);
  }}
  const meta = CATS.find(c => c.category === cat);
  const dl = document.getElementById("dlAll");
  if (meta) {{
    dl.href = meta.file;
    dl.download = meta.slug + ".ics";
    dl.textContent = "Download season · " + meta.category;
  }}
  syncCatQuery();
}}
function renderMonth(){{
  document.getElementById("monthLabel").textContent = view.toLocaleString("en", {{month:"long", year:"numeric"}});
  document.getElementById("calDows").innerHTML = DOW.map(d => '<div class="cal-dow">'+d+'</div>').join("");
  const grid = document.getElementById("calGrid");
  grid.innerHTML = "";
  const first = new Date(view.getFullYear(), view.getMonth(), 1);
  let startPad = (first.getDay()+6)%7;
  const daysInMonth = new Date(view.getFullYear(), view.getMonth()+1, 0).getDate();
  const byDate = {{}};
  gamesFor(cat).forEach(g => {{
    if (!byDate[g.date]) byDate[g.date] = [];
    byDate[g.date].push(g);
  }});
  const monthKeys = Object.keys(byDate).filter(k => {{
    const parts = k.split("-").map(Number);
    return parts[0] === view.getFullYear() && parts[1] === view.getMonth()+1;
  }});
  const empty = document.getElementById("calEmpty");
  if (!monthKeys.length) {{
    const upcoming = gamesFor(cat).map(g => g.date).filter(Boolean).sort().find(d => d >= TODAY);
    empty.hidden = false;
    empty.textContent = upcoming
      ? ("No games in this month for "+cat+". Next game: "+upcoming+".")
      : ("No games in this month for "+cat+".");
  }} else {{
    empty.hidden = true;
    empty.textContent = "";
  }}
  for (let i=0;i<startPad;i++) {{
    const cell = document.createElement("div");
    cell.className = "cal-cell mute";
    cell.setAttribute("aria-hidden", "true");
    grid.appendChild(cell);
  }}
  for (let day=1; day<=daysInMonth; day++) {{
    const d = new Date(view.getFullYear(), view.getMonth(), day);
    const key = ymd(d);
    const list = byDate[key] || [];
    const isToday = key === TODAY;
    const cell = document.createElement(list.length ? "button" : "div");
    if (list.length) cell.type = "button";
    cell.className = "cal-cell"
      + (list.length ? " has" : "")
      + (selectedDate===key ? " is-sel" : "")
      + (isToday ? " is-today" : "");
    let html = '<div class="d">'+day+'</div>';
    if (list.length) {{
      const n = list.length;
      html += '<div class="cnt" aria-hidden="true">'+n+'</div>';
      html += '<div class="glist">' + list.map(g =>
        '<div class="g"><span class="t">'+escapeHtml(g.time||"TBD")+'</span>vs '+escapeHtml(shortOpp(g.opponent))+'</div>'
      ).join("") + '</div>';
      cell.setAttribute("aria-label", key + ": " + n + " game" + (n===1?"":"s") + " · " + cat);
      cell.setAttribute("aria-pressed", selectedDate===key ? "true" : "false");
      cell.onclick = () => {{ selectedDate = key; renderAll(); }};
    }} else if (isToday) {{
      cell.setAttribute("aria-label", key + " · today");
    }}
    cell.innerHTML = html;
    grid.appendChild(cell);
  }}
}}
function renderDay(){{
  const panel = document.getElementById("dayPanel");
  if (!selectedDate) {{ panel.hidden = true; return; }}
  const list = gamesFor(cat, selectedDate);
  panel.hidden = false;
  document.getElementById("dayTitle").textContent = selectedDate;
  document.getElementById("dayMeta").textContent = list.length + " game(s) · " + cat; // textContent = safe
  document.getElementById("dlDay").onclick = () => {{
    downloadIcs(slugFor(cat) + "-" + selectedDate + ".ics", icsFile(list, cat));
  }};
  const box = document.getElementById("dayGames");
  box.innerHTML = list.map(g => (
    '<article class="game">'+
      '<div class="time">'+escapeHtml(g.time||"TBD")+'</div>'+
      '<div>'+
        '<div class="league">'+escapeHtml(g.competitionName||"")+'</div>'+
        '<div class="matchup">'+escapeHtml(g.homeAway||"")+' vs <strong>'+escapeHtml(g.opponent||"")+'</strong></div>'+
        '<div class="venue">'+escapeHtml(g.venue||"")+' <span class="id">#'+escapeHtml(g.id)+'</span></div>'+
        '<div style="margin-top:8px"><button type="button" class="btn btn-ghost" data-gid="'+escapeHtml(g.id)+'">Download this game (.ics)</button></div>'+
      '</div>'+
    '</article>'
  )).join("");
  box.querySelectorAll("button[data-gid]").forEach(btn => {{
    btn.onclick = (ev) => {{
      ev.stopPropagation();
      const g = list.find(x => String(x.id) === btn.getAttribute("data-gid"));
      if (!g) return;
      downloadIcs(slugFor(cat) + "-game-" + g.id + ".ics", icsFileSingle(g, cat));
    }};
  }});
}}
function renderAll(){{ renderCats(); renderMonth(); renderDay(); }}
document.getElementById("prevMonth").onclick = () => {{ view = new Date(view.getFullYear(), view.getMonth()-1, 1); selectedDate=null; renderAll(); }};
document.getElementById("nextMonth").onclick = () => {{ view = new Date(view.getFullYear(), view.getMonth()+1, 1); selectedDate=null; renderAll(); }};
document.getElementById("thisMonth").onclick = () => {{ view = new Date(anchor.getFullYear(), anchor.getMonth(), 1); selectedDate=null; renderAll(); }};
renderAll();
</script>
"""
    return shell(
        "Calendar",
        body,
        last_run=last_run,
        next_run=next_run,
        active="calendar",
        hero_title="Calendar",
        hero_lead="Download one game, one day, or the full season.",
    )



def render_all(
    snapshot_path: Path,
    out_dir: Path,
    pages_base_url: str = "",
    today: str | None = None,
    last_success_path: Path | None = None,
    executed_at: datetime | None = None,
    cfg: dict[str, Any] | None = None,
    changes_path: Path | None = None,
) -> dict[str, Any]:
    if cfg is not None:
        set_active_cfg(cfg)
    else:
        set_active_cfg(load_config())
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    games = load_games(snapshot)
    out_dir.mkdir(parents=True, exist_ok=True)
    d_today = date.fromisoformat(today) if today else None
    last_run, next_run = resolve_timing(snapshot, last_success_path, executed_at)
    mon, sun = current_week(d_today)
    week_dates = {(mon + timedelta(days=i)).isoformat() for i in range(7)}
    week_n = sum(1 for g in games if g.get("date") in week_dates)
    clash_cats = _clash_categories(games)
    ics_index = write_category_ics(games, out_dir / "ics")
    club = club_info()
    changes_report = load_changes_report(
        changes_path,
        fallback_path=out_dir / "changes.json",
        snapshot=snapshot,
    )

    (out_dir / "index.html").write_text(
        build_index(
            snapshot,
            pages_base_url,
            last_run=last_run,
            next_run=next_run,
            weekend_n=week_n,
            clash_bands=clash_cats,
            games=games,
            today=d_today or date.today(),
        ),
        encoding="utf-8",
    )
    (out_dir / "clashes.html").write_text(
        build_clashes(games, last_run=last_run, next_run=next_run), encoding="utf-8"
    )
    (out_dir / "weekend.html").write_text(
        build_weekend(games, d_today, pages_base_url, last_run=last_run, next_run=next_run),
        encoding="utf-8",
    )
    (out_dir / "calendar.html").write_text(
        build_calendar(
            games,
            ics_index,
            last_run=last_run,
            next_run=next_run,
            today=d_today or date.today(),
        ),
        encoding="utf-8",
    )
    (out_dir / "changes.html").write_text(
        build_changes(changes_report, last_run=last_run, next_run=next_run),
        encoding="utf-8",
    )
    # Durable copy for Pages / local rebuild without a fresh diff
    (out_dir / "changes.json").write_text(
        json.dumps(changes_report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    meta = {
        "probedAt": snapshot.get("probedAt"),
        "gameCount": len(snapshot.get("games") or []),
        "clubId": club.get("id"),
        "clubName": club.get("name"),
        "timezone": schedule_timezone(),
        "week": {"mon": mon.isoformat(), "sun": sun.isoformat(), "games": week_n},
        # legacy alias (same bounds as week)
        "weekend": {"sat": mon.isoformat(), "sun": sun.isoformat(), "games": week_n},
        "clashCategories": clash_cats,
        "calendarCategories": ics_index,
        "pages": [
            "index.html",
            "weekend.html",
            "clashes.html",
            "calendar.html",
            "changes.html",
        ],
        "nav": ["Home", "This week", "Clashes", "Calendar", "Changes"],
        "changes": {
            "hasChanges": bool(changes_report.get("hasChanges")),
            "comparedAt": changes_report.get("comparedAt"),
            "oldProbedAt": changes_report.get("oldProbedAt"),
            "newProbedAt": changes_report.get("newProbedAt"),
            "counts": changes_report.get("counts"),
            "note": "Reflects the last successful watch diff (pages/changes.json).",
        },
        "pagesBaseUrl": pages_base_url or None,
        "lastRun": last_run.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "nextRun": next_run.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "lastRunLabel": fmt_stamp(last_run),
        "nextRunLabel": fmt_stamp(next_run),
        "theme": f"{club.get('slug') or 'club'}-dark-lime",
        "fortress": {
            "csp": bool(active_cfg().get("fortress", {}).get("csp", True)),
            "minGameRatio": active_cfg().get("fortress", {}).get("minGameRatio"),
        },
    }
    (out_dir / "site-meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return meta


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", type=Path, required=True)
    ap.add_argument("--out-dir", type=Path, required=True)
    ap.add_argument("--pages-base-url", default="", help="e.g. https://USER.github.io/REPO")
    ap.add_argument("--today", default=None, help="YYYY-MM-DD override for weekend calc")
    ap.add_argument("--last-success", type=Path, default=None)
    ap.add_argument(
        "--changes-json",
        type=Path,
        default=None,
        help="Change report from compare_snapshots (written to pages/changes.json)",
    )
    ap.add_argument("--config", type=Path, default=None)
    args = ap.parse_args()
    cfg = load_config(str(args.config) if args.config else None)
    set_active_cfg(cfg)
    meta = render_all(
        args.snapshot,
        args.out_dir,
        sanitize_pages_base_url(args.pages_base_url),
        args.today,
        last_success_path=args.last_success,
        executed_at=datetime.now(timezone.utc),
        cfg=cfg,
        changes_path=args.changes_json,
    )
    print(json.dumps(meta, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
