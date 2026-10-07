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
# U12: Bully = A, Assist = B, Mädchen, Alpencup (OÖ) — same-day only (no kickoff time check).
RELATED_SQUAD_GROUPS = [
    {
        "id": "u12-groups",
        "label": "U12 group overlap",
        "detail": "Assist · Bully · Mädchen · Alpencup — separate squads, flagged when any two play the same day",
        "categories": ("U12 Assist", "U12 Bully", "U12 Mädchen", "U12 Alpencup"),
        "timeClash": False,
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

# Defaults (overridden by config.yaml)
DEFAULT_REMINDER_CATEGORIES = frozenset({"Bundesliga", "Adults Grossfeld", "Adults Kleinfeld"})
DEFAULT_REMINDER_WEEKDAYS = (("mon", 0, "Monday"), ("wed", 2, "Wednesday"), ("fri", 4, "Friday"))
DEFAULT_REMINDER_HOUR = 9

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


def reminder_settings() -> tuple[frozenset[str], tuple[tuple[str, int, str], ...], int]:
    rem = active_cfg().get("reminders") or {}
    cats = frozenset(rem.get("categories") or DEFAULT_REMINDER_CATEGORIES)
    hour = int(rem.get("hour") if rem.get("hour") is not None else DEFAULT_REMINDER_HOUR)
    wd_map = {"mon": (0, "Monday"), "tue": (1, "Tuesday"), "wed": (2, "Wednesday"),
              "thu": (3, "Thursday"), "fri": (4, "Friday"), "sat": (5, "Saturday"), "sun": (6, "Sunday")}
    raw = rem.get("weekdays") or ["mon", "wed", "fri"]
    weekdays: list[tuple[str, int, str]] = []
    for slug in raw:
        key = str(slug).lower()[:3]
        if key in wd_map:
            n, label = wd_map[key]
            weekdays.append((key, n, label))
    if not weekdays:
        weekdays = list(DEFAULT_REMINDER_WEEKDAYS)
    return cats, tuple(weekdays), hour


def ics_uid_domain() -> str:
    club = club_info()
    url = club.get("url") or ""
    host = urlparse(url).hostname if url else None
    if host:
        return host.lstrip("www.")
    slug = club.get("slug") or "club"
    return f"{slug}.at"


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
    today = today or date.today()
    wd = today.weekday()
    if wd == 5:
        sat = today
    elif wd == 6:
        sat = today - timedelta(days=1)
    else:
        sat = today + timedelta(days=(5 - wd))
    return sat, sat + timedelta(days=1)


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
.pages-nav{
  display:flex;flex-wrap:wrap;gap:8px;margin:0 0 16px;padding:10px 0 2px;
  border-bottom:1px solid rgba(255,255,255,.08);
}
.pages-nav a{
  color:var(--lime);font:700 13px/1 var(--font-display);letter-spacing:.08em;text-transform:uppercase;
  padding:8px 2px;margin-right:10px;border-bottom:2px solid transparent;
}
.pages-nav a.is-active{color:var(--text);border-bottom-color:var(--lime)}
.pages-nav a:hover{color:#e6ff66}
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
  color:var(--muted);font-size:13px;display:flex;flex-wrap:wrap;gap:10px 18px;justify-content:space-between;
}
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
        ("weekend", "weekend.html", "Weekend"),
        ("clashes", "clashes.html", "Clashes"),
        ("calendar", "calendar.html", "Calendar"),
    ]
    nav = []
    pages_nav = []
    for key, href, label in nav_items:
        cls = " is-active" if active == key else ""
        nav.append(f'<a class="{cls.strip()}" href="{href}">{label}</a>')
        pages_nav.append(f'<a class="{cls.strip()}" href="{href}">{label}</a>')
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
<html lang="de">
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
  <nav class="pages-nav" aria-label="Pages">{''.join(pages_nav)}</nav>
  <main class="main">{body}</main>
  <footer class="footer">
    <div>Data from FloorballFlash · club {escape(str(club_id))}</div>
    <div><a href="{escape(club_url, quote=True)}" rel="noopener">{escape(urlparse(club_url).hostname or club_url)}</a></div>
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


def _game_block(g: dict[str, Any], *, show_date: bool = False) -> str:
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
        f"</div></article>"
    )


def _times_overlap(t1: int, t2: int, window_min: int = 90) -> bool:
    return not (t1 + window_min <= t2 or t2 + window_min <= t1)


def detect_clashes(games: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Same-squad clashes + related multi-squad awareness (e.g. all U12 groups).

    Calendars stay per category. Related groups add informational same-day overlaps
    when any two (or more) listed squads play. U12 uses same-day only — no kickoff
    time-overlap check (related or same-squad).
    """
    by_date: dict[str, list] = defaultdict(list)
    for g in games:
        by_date[g["date"]].append(g)

    same: dict[str, dict[str, list]] = {c: {"sameDay": [], "timeClash": []} for c in CATEGORY_ORDER}
    related: list[dict[str, Any]] = []

    for dt, gs in by_date.items():
        by_cat: dict[str, list] = defaultdict(list)
        for g in gs:
            by_cat[g["_category"]].append(g)

        # Same squad: 2+ competitions on one day
        for cat, cgs in by_cat.items():
            leagues = {g["competitionName"] for g in cgs}
            if len(cgs) >= 2 and len(leagues) > 1:
                same[cat]["sameDay"].append(
                    {
                        "date": dt,
                        "games": sorted(cgs, key=lambda x: (x.get("time") or "99:99", x["id"])),
                        "leagues": sorted(leagues),
                    }
                )

        # Same squad: overlapping kickoffs across competitions (not used for U12)
        seen_pairs: set[tuple[int, int]] = set()
        for cat, cgs in by_cat.items():
            if cat.startswith("U12"):
                continue
            timed = [(g, parse_time(g.get("time"))) for g in cgs if parse_time(g.get("time")) is not None]
            for i in range(len(timed)):
                for j in range(i + 1, len(timed)):
                    g1, t1 = timed[i]
                    g2, t2 = timed[j]
                    if g1["competitionName"] == g2["competitionName"]:
                        continue
                    if not _times_overlap(t1, t2):
                        continue
                    key = tuple(sorted([g1["id"], g2["id"]]))
                    if key in seen_pairs:
                        continue
                    seen_pairs.add(key)
                    same[cat]["timeClash"].append({"date": dt, "a": g1, "b": g2})

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
            # Optional kickoff overlap across related squads (disabled for U12)
            if group.get("timeClash", True) is False:
                continue
            timed_by_cat = [
                (c, [(g, parse_time(g.get("time"))) for g in pool if parse_time(g.get("time")) is not None])
                for c, pool in present
            ]
            for i in range(len(timed_by_cat)):
                for j in range(i + 1, len(timed_by_cat)):
                    for g1, t1 in timed_by_cat[i][1]:
                        for g2, t2 in timed_by_cat[j][1]:
                            if not _times_overlap(t1, t2):
                                continue
                            related.append(
                                {
                                    "groupId": group["id"],
                                    "label": group["label"],
                                    "detail": group["detail"],
                                    "kind": "timeClash",
                                    "date": dt,
                                    "squads": [timed_by_cat[i][0], timed_by_cat[j][0]],
                                    "a": g1,
                                    "b": g2,
                                }
                            )

    return {"same": same, "related": related}


def build_clashes(
    games: list[dict[str, Any]],
    last_run: datetime | None = None,
    next_run: datetime | None = None,
) -> str:
    """Same-squad clashes + U12 group overlap awareness."""
    detected = detect_clashes(games)
    structure = detected["same"]
    related = detected["related"]

    active = [c for c in CATEGORY_ORDER if structure[c]["sameDay"] or structure[c]["timeClash"]]
    related_groups = {g["id"]: g for g in RELATED_SQUAD_GROUPS}
    related_active = sorted({r["groupId"] for r in related})

    if not active and not related_active:
        body = (
            '<div class="empty"><strong>No upcoming squad clashes or U12 group overlaps.</strong> '
            "Same-squad conflicts and U12 Assist/Bully/Mädchen/Alpencup overlaps are listed here when they appear.</div>"
        )
        return shell(
            "Clashes",
            body,
            last_run=last_run,
            next_run=next_run,
            active="clashes",
            hero_title="Clashes",
            hero_lead="Same squad conflicts, plus U12 group overlaps (Assist · Bully · Mädchen · Alpencup).",
        )

    parts = [
        '<p class="lead">Same-squad conflicts, plus U12 group overlap awareness '
        "(Assist · Bully · Mädchen · Alpencup — separate calendars, flagged when any two play the same day).</p>",
        '<div class="chips">',
    ]
    for gid in related_active:
        n = sum(1 for r in related if r["groupId"] == gid)
        parts.append(
            f'<a class="chip soft" style="border-color:#f5a524;color:#f5a524" '
            f'href="#{gid}">{escape(related_groups[gid]["label"])} · {n}</a>'
        )
    for c in active:
        n = len(structure[c]["sameDay"]) + len(structure[c]["timeClash"])
        accent = CATEGORY_ACCENT.get(c, "#ccff00")
        parts.append(
            f'<a class="chip soft" style="border-color:{accent};color:{accent}" '
            f'href="#{category_slug(c)}">{escape(c)} · {n}</a>'
        )
    parts.append("</div>")

    # Related multi-squad sections first (awareness)
    for gi, gid in enumerate(related_active):
        open_attr = " open" if gi == 0 and not active else (" open" if gi == 0 else "")
        items = [r for r in related if r["groupId"] == gid]
        meta = related_groups[gid]
        same_day = [r for r in items if r["kind"] == "sameDay"]
        time_x = [r for r in items if r["kind"] == "timeClash"]
        n = len(same_day) + len(time_x)
        parts.append(
            f'<details class="band"{open_attr} id="{gid}">'
            f'<summary><span style="color:#f5a524">{escape(meta["label"])}</span>'
            f'<span class="meta" style="color:var(--muted);font:600 12px var(--font-body)">{n} · '
            f'{escape(meta["detail"])} <span class="chev">▸</span></span></summary>'
            f'<div class="band-body">'
        )
        for item in sorted(same_day, key=lambda x: x["date"]):
            squads = item.get("squads") or []
            squad_meta = " + ".join(squads) if squads else "related U12 squads"
            parts.append(
                f'<div class="panel"><div class="panel-h">'
                f'<span><span class="badge">U12 same day</span>{escape(fmt_date(item["date"]))}</span>'
                f'<span class="meta">{escape(squad_meta)}</span></div>'
            )
            for g in item["games"]:
                parts.append(_game_block(g))
            parts.append("</div>")
        by_dt: dict[str, list] = defaultdict(list)
        for p in time_x:
            by_dt[p["date"]].append(p)
        for dt in sorted(by_dt):
            parts.append(
                f'<div class="panel"><div class="panel-h">'
                f'<span><span class="badge warn">U12 time overlap</span>{escape(fmt_date(dt))}</span></div>'
            )
            for p in by_dt[dt]:
                parts.append(_game_block(p["a"]))
                parts.append(
                    '<div style="text-align:center;color:var(--warn);font:700 12px var(--font-display);'
                    'letter-spacing:.12em;padding:4px 0">U12 OVERLAP ≈ 90 MIN</div>'
                )
                parts.append(_game_block(p["b"]))
            parts.append("</div>")
        parts.append("</div></details>")

    for i, cat in enumerate(active):
        open_attr = " open" if i == 0 and not related_active else ""
        a, b = structure[cat]["sameDay"], structure[cat]["timeClash"]
        n = len(a) + len(b)
        accent = CATEGORY_ACCENT.get(cat, "#ccff00")
        parts.append(
            f'<details class="band"{open_attr} id="{category_slug(cat)}">'
            f'<summary><span style="color:{accent}">{escape(cat)}</span>'
            f'<span class="meta" style="color:var(--muted);font:600 12px var(--font-body)">{n} conflict(s) '
            f'<span class="chev">▸</span></span></summary>'
            f'<div class="band-body">'
        )
        for item in a:
            parts.append(
                f'<div class="panel"><div class="panel-h">'
                f'<span><span class="badge">Same day</span>{escape(fmt_date(item["date"]))}</span>'
                f'<span class="meta">{escape(" · ".join(item["leagues"]))}</span></div>'
            )
            for g in item["games"]:
                parts.append(_game_block(g))
            parts.append("</div>")
        by_dt = defaultdict(list)
        for p in b:
            by_dt[p["date"]].append(p)
        for dt in sorted(by_dt):
            parts.append(
                f'<div class="panel"><div class="panel-h">'
                f'<span><span class="badge warn">Time clash</span>{escape(fmt_date(dt))}</span></div>'
            )
            for p in by_dt[dt]:
                parts.append(_game_block(p["a"]))
                parts.append(
                    '<div style="text-align:center;color:var(--warn);font:700 12px var(--font-display);'
                    'letter-spacing:.12em;padding:4px 0">OVERLAP ≈ 90 MIN</div>'
                )
                parts.append(_game_block(p["b"]))
            parts.append("</div>")
        parts.append("</div></details>")

    return shell(
        "Clashes",
        "\n".join(parts),
        last_run=last_run,
        next_run=next_run,
        active="clashes",
        hero_title="Clashes",
        hero_lead="Same-squad conflicts, plus U12 group overlaps (Assist · Bully · Mädchen · Alpencup).",
    )


def build_weekend(
    games: list[dict[str, Any]],
    today: date | None = None,
    pages_base_url: str = "",
    last_run: datetime | None = None,
    next_run: datetime | None = None,
) -> str:
    sat, sun = next_weekend(today)
    sat_s, sun_s = sat.isoformat(), sun.isoformat()
    weekend_games = [g for g in games if g.get("date") in (sat_s, sun_s)]
    weekend_games.sort(key=lambda g: (g.get("date") or "", g.get("time") or "99:99", g["id"]))

    by_band: dict[str, list] = defaultdict(list)
    by_cat: dict[str, list] = defaultdict(list)
    for g in weekend_games:
        by_band[g["_band"]].append(g)
        by_cat[g["_category"]].append(g)

    chips = []
    for c in CATEGORY_ORDER:
        if c in by_cat:
            accent = CATEGORY_ACCENT.get(c, "#ccff00")
            chips.append(
                f'<span class="chip" style="background:{accent}">{escape(c)} · {len(by_cat[c])}</span>'
            )

    parts = [
        f'<p class="lead"><strong style="color:var(--text)">{escape(fmt_date(sat_s))}</strong>'
        f' – <strong style="color:var(--text)">{escape(fmt_date(sun_s))}</strong>'
        f' · {len(weekend_games)} game(s)</p>',
        f'<div class="chips">{"".join(chips)}</div>' if chips else "",
    ]
    if not weekend_games:
        parts.append(
            f'<div class="empty"><strong>No {escape(club_info().get("name") or "club")} games</strong> scheduled for this weekend.</div>'
        )
    else:
        for day in (sat_s, sun_s):
            day_games = [g for g in weekend_games if g["date"] == day]
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
        "Next weekend",
        "\n".join(p for p in parts if p),
        last_run=last_run,
        next_run=next_run,
        active="weekend",
        hero_title="Next match weekend",
        hero_lead="League games Saturday–Sunday.",
    )


def build_index(
    snapshot: dict[str, Any],
    pages_base_url: str = "",
    last_run: datetime | None = None,
    next_run: datetime | None = None,
    weekend_n: int = 0,
    clash_bands: list[str] | None = None,
) -> str:
    n = len(snapshot.get("games") or [])
    bands = clash_bands or []
    bands_stat = escape(", ".join(bands)) if bands else "None"
    body = f"""
<div class="stats">
  <div class="stat"><span>Games in baseline</span><b>{n}</b></div>
  <div class="stat"><span>This weekend</span><b>{weekend_n}</b></div>
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
        bucket = detected["same"][cat]
        if bucket["sameDay"] or bucket["timeClash"]:
            labels.append(cat)
    return labels


def _ics_escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def _vevent(g: dict[str, Any], category: str) -> str:
    gid = g["id"]
    ds = g.get("date") or ""
    tm = g.get("time")
    mins = parse_time(tm)
    tzid = schedule_timezone()
    domain = ics_uid_domain()
    if mins is not None:
        start = datetime.strptime(f"{ds} {tm}", "%Y-%m-%d %H:%M")
        end = start + timedelta(minutes=90)
        dtstart = start.strftime("%Y%m%dT%H%M%S")
        dtend = end.strftime("%Y%m%dT%H%M%S")
        time_lines = f"DTSTART;TZID={tzid}:{dtstart}\nDTEND;TZID={tzid}:{dtend}"
    else:
        ymd = ds.replace("-", "")
        time_lines = f"DTSTART;VALUE=DATE:{ymd}\nDTEND;VALUE=DATE:{ymd}"

    summary = f"{category}: vs {g.get('opponent') or '?'}"
    desc_parts = [
        g.get("competitionName") or "",
        f"{g.get('homeAway') or ''} vs {g.get('opponent') or ''}".strip(),
        f"Time: {tm or 'TBD'} ({tzid})",
        f"Venue: {g.get('venue') or 'TBD'}",
        f"Game #{gid}",
    ]
    desc = _ics_escape("\n".join(p for p in desc_parts if p))
    loc = _ics_escape(g.get("venue") or "")
    return (
        "BEGIN:VEVENT\n"
        f"UID:{club_info().get('slug') or 'club'}-{gid}@{domain}\n"
        f"{time_lines}\n"
        f"SUMMARY:{_ics_escape(summary)}\n"
        f"DESCRIPTION:{desc}\n"
        f"LOCATION:{loc}\n"
        "END:VEVENT"
    )


def previous_weekday(game_day: date, weekday: int) -> date:
    """Most recent `weekday` (Mon=0) strictly before `game_day`."""
    days_back = (game_day.weekday() - weekday) % 7
    if days_back == 0:
        days_back = 7
    return game_day - timedelta(days=days_back)


def _reminder_vevents(g: dict[str, Any], category: str) -> list[str]:
    """Reminder events before the game (configured adult categories)."""
    ds = g.get("date") or ""
    try:
        game_day = date.fromisoformat(ds)
    except Exception:
        return []
    _, weekdays, hour = reminder_settings()
    tzid = schedule_timezone()
    domain = ics_uid_domain()
    slug_prefix = club_info().get("slug") or "club"
    tm = g.get("time") or "TBD"
    opponent = g.get("opponent") or "?"
    venue = g.get("venue") or "TBD"
    game_label = fmt_date(ds)
    out: list[str] = []
    for slug, weekday, label in weekdays:
        remind_day = previous_weekday(game_day, weekday)
        start = datetime(remind_day.year, remind_day.month, remind_day.day, hour, 0, 0)
        end = start + timedelta(minutes=15)
        summary = f"Reminder ({label[:3]}): {category} vs {opponent}"
        desc = "\n".join(
            [
                f"{label} reminder before the game.",
                f"Game: {game_label} · {tm}",
                f"{g.get('homeAway') or ''} vs {opponent}".strip(),
                f"Venue: {venue}",
                f"Competition: {g.get('competitionName') or ''}",
                f"Game #{g['id']}",
                f"Timezone: {tzid}",
            ]
        )
        out.append(
            "BEGIN:VEVENT\n"
            f"UID:{slug_prefix}-{g['id']}-remind-{slug}@{domain}\n"
            f"DTSTART;TZID={tzid}:{start.strftime('%Y%m%dT%H%M%S')}\n"
            f"DTEND;TZID={tzid}:{end.strftime('%Y%m%dT%H%M%S')}\n"
            f"SUMMARY:{_ics_escape(summary)}\n"
            f"DESCRIPTION:{_ics_escape(desc)}\n"
            f"LOCATION:{_ics_escape(venue)}\n"
            "BEGIN:VALARM\n"
            "TRIGGER:-PT0S\n"
            "ACTION:DISPLAY\n"
            f"DESCRIPTION:{_ics_escape(summary)}\n"
            "END:VALARM\n"
            "END:VEVENT"
        )
    return out


def build_ics(games: list[dict[str, Any]], category: str) -> str:
    rem_cats, _, _ = reminder_settings()
    club_name = club_info().get("name") or "Club"
    tzid = schedule_timezone()
    events: list[str] = []
    for g in games:
        events.append(_vevent(g, category))
        if category in rem_cats:
            events.extend(_reminder_vevents(g, category))
    cal_name = f"{club_name} {category}"
    if category in rem_cats:
        cal_name += " (+ reminders)"
    return (
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
        path.write_text(build_ics(cgs, cat), encoding="utf-8")
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
    rem_cats, rem_days, rem_hour = reminder_settings()
    # JS weekday: Sun=0 … Sat=6; Python Mon=0 … Sun=6 → js = (py+1)%7
    rem_days_js = [
        {"slug": s, "weekday": (py_wd + 1) % 7, "label": label}
        for s, py_wd, label in rem_days
    ]
    club = club_info()
    club_name_js = json.dumps(club.get("name") or "Club")
    club_slug_js = json.dumps(club.get("slug") or "club")
    uid_domain_js = json.dumps(ics_uid_domain())
    tzid_js = json.dumps(schedule_timezone())
    rem_cats_js = json.dumps(sorted(rem_cats))
    rem_days_json = json.dumps(rem_days_js, ensure_ascii=False)
    # Escape VTIMEZONE for embedding inside a JS string literal via JSON
    vtz_js = json.dumps(VIENNA_VTIMEZONE)

    extra_css = """
.cal-wrap{display:grid;gap:14px}
.cat-bar{display:flex;flex-wrap:wrap;gap:8px}
.cat-bar button{
  border:1px solid var(--line);background:rgba(255,255,255,.03);color:var(--text);
  border-radius:999px;padding:8px 12px;cursor:pointer;
  font:700 12px/1 var(--font-display);letter-spacing:.06em;text-transform:uppercase;
}
.cat-bar button.is-on{background:var(--lime);color:#000;border-color:var(--lime)}
.cal-toolbar{display:flex;flex-wrap:wrap;gap:10px;align-items:center;justify-content:space-between}
.cal-grid{
  display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:8px;
}
.cal-dow{text-align:center;font:700 11px/1 var(--font-display);letter-spacing:.08em;color:var(--muted);padding:6px 0}
.cal-cell{
  min-height:110px;border:1px solid rgba(255,255,255,.08);border-radius:10px;
  background:rgba(0,0,0,.28);padding:6px;cursor:default;position:relative;
  display:flex;flex-direction:column;gap:4px;
}
.cal-cell.has{
  cursor:pointer;border-color:rgba(204,255,0,.55);background:rgba(204,255,0,.10);
  box-shadow:inset 0 0 0 1px rgba(204,255,0,.25);
}
.cal-cell.has:hover,.cal-cell.is-sel{background:rgba(204,255,0,.18);border-color:var(--lime)}
.cal-cell .d{font:700 13px/1 var(--font-display);color:var(--muted)}
.cal-cell.has .d{color:var(--lime)}
.cal-cell .glist{display:flex;flex-direction:column;gap:3px;margin-top:2px}
.cal-cell .g{
  font:600 11px/1.25 var(--font-body);color:var(--text);
  background:rgba(0,0,0,.45);border-left:3px solid var(--lime);
  padding:3px 5px;border-radius:4px;
}
.cal-cell .g .t{color:var(--lime);font-weight:700;margin-right:4px}
.cal-cell.mute{opacity:.28;min-height:48px}
.day-panel{margin-top:8px}
.day-panel[hidden]{display:none}
.dl-row{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0 12px}
@media (max-width:800px){
  .cal-grid{gap:5px}
  .cal-cell{min-height:96px;padding:4px}
  .cal-cell .g{font-size:10px;padding:2px 4px}
}
"""

    body = f"""
<style>{extra_css}</style>
<p class="lead">Choose a category. Game days show each kickoff in the cell. Click a day for downloads: one game, that day, or the whole season.</p>
<div class="cal-wrap">
  <div class="cat-bar" id="catBar"></div>
  <div class="cal-toolbar">
    <div>
      <button type="button" class="btn btn-ghost" id="prevMonth">←</button>
      <strong id="monthLabel" style="margin:0 10px;font:700 1.1rem var(--font-display);letter-spacing:.06em;text-transform:uppercase"></strong>
      <button type="button" class="btn btn-ghost" id="nextMonth">→</button>
      <button type="button" class="btn btn-ghost" id="thisMonth">Today</button>
    </div>
    <div style="display:flex;flex-wrap:wrap;gap:8px">
      <a class="btn btn-lime" id="dlAll" href="#">Download season (.ics)</a>
    </div>
  </div>
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
const UID_DOMAIN = {uid_domain_js};
const TZID = {tzid_js};
const VTIMEZONE = {vtz_js};
const REMINDER_CATS = new Set({rem_cats_js});
const REMINDER_DAYS = {rem_days_json};
const REMINDER_HOUR = {rem_hour};
const DOW = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"];
let cat = (CATS[0] && CATS[0].category) || null;
let anchor = new Date(TODAY + "T12:00:00");
let view = new Date(anchor.getFullYear(), anchor.getMonth(), 1);
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
function previousWeekday(gameDate, jsWeekday){{
  const d = new Date(gameDate+"T00:00:00");
  const cur = d.getDay();
  let back = (cur - jsWeekday + 7) % 7;
  if (back === 0) back = 7;
  d.setDate(d.getDate() - back);
  return d;
}}
function vevent(g){{
  let timeLines;
  if (g.time) {{
    const [hh, mm] = String(g.time).split(":").map(Number);
    const ymdParts = g.date.split("-").map(Number);
    const fmtLocal = (Y,M,D,H,Mi) => Y+pad(M)+pad(D)+"T"+pad(H)+pad(Mi)+"00";
    const startS = fmtLocal(ymdParts[0], ymdParts[1], ymdParts[2], hh, mm);
    let endH = hh, endM = mm + 90;
    endH += Math.floor(endM / 60); endM = endM % 60;
    let endD = ymdParts[2], endMo = ymdParts[1], endY = ymdParts[0];
    if (endH >= 24) {{ endH -= 24; endD += 1; }}
    const endS = fmtLocal(endY, endMo, endD, endH, endM);
    timeLines = "DTSTART;TZID="+TZID+":"+startS+"\\nDTEND;TZID="+TZID+":"+endS;
  }} else {{
    const y = g.date.replace(/-/g,"");
    timeLines = "DTSTART;VALUE=DATE:"+y+"\\nDTEND;VALUE=DATE:"+y;
  }}
  const summary = icsEscape(g.category+": vs "+(g.opponent||"?"));
  const desc = icsEscape([g.competitionName, (g.homeAway||"")+" vs "+(g.opponent||""), "Time: "+(g.time||"TBD")+" ("+TZID+")", "Venue: "+(g.venue||"TBD"), "Game #"+g.id].join("\\n"));
  return "BEGIN:VEVENT\\nUID:"+CLUB_SLUG+"-"+g.id+"@"+UID_DOMAIN+"\\n"+timeLines+"\\nSUMMARY:"+summary+"\\nDESCRIPTION:"+desc+"\\nLOCATION:"+icsEscape(g.venue||"")+"\\nEND:VEVENT";
}}
function reminderEvents(g){{
  if (!REMINDER_CATS.has(g.category)) return [];
  const out = [];
  REMINDER_DAYS.forEach(r => {{
    const day = previousWeekday(g.date, r.weekday);
    const fmt = d => d.getFullYear()+pad(d.getMonth()+1)+pad(d.getDate())+"T"+pad(REMINDER_HOUR)+"0000";
    const endFmt = d => {{
      const e = new Date(d.getFullYear(), d.getMonth(), d.getDate(), REMINDER_HOUR, 15, 0);
      return e.getFullYear()+pad(e.getMonth()+1)+pad(e.getDate())+"T"+pad(e.getHours())+pad(e.getMinutes())+"00";
    }};
    const summary = icsEscape("Reminder ("+r.label.slice(0,3)+"): "+g.category+" vs "+(g.opponent||"?"));
    const desc = icsEscape([
      r.label+" reminder before the game.",
      "Game: "+g.date+" · "+(g.time||"TBD"),
      (g.homeAway||"")+" vs "+(g.opponent||""),
      "Venue: "+(g.venue||"TBD"),
      "Competition: "+(g.competitionName||""),
      "Game #"+g.id,
      "Timezone: "+TZID
    ].join("\\n"));
    out.push(
      "BEGIN:VEVENT\\nUID:"+CLUB_SLUG+"-"+g.id+"-remind-"+r.slug+"@"+UID_DOMAIN+"\\n"+
      "DTSTART;TZID="+TZID+":"+fmt(day)+"\\nDTEND;TZID="+TZID+":"+endFmt(day)+"\\nSUMMARY:"+summary+"\\nDESCRIPTION:"+desc+
      "\\nLOCATION:"+icsEscape(g.venue||"")+"\\nBEGIN:VALARM\\nTRIGGER:-PT0S\\nACTION:DISPLAY\\nDESCRIPTION:"+summary+"\\nEND:VALARM\\nEND:VEVENT"
    );
  }});
  return out;
}}
function icsFile(list, catName){{
  const events = [];
  list.forEach(g => {{ events.push(vevent(g)); reminderEvents(g).forEach(e => events.push(e)); }});
  let name = CLUB_NAME+" "+catName;
  if (REMINDER_CATS.has(catName)) name += " (+ reminders)";
  return "BEGIN:VCALENDAR\\nVERSION:2.0\\nPRODID:-//"+CLUB_NAME+"//Schedule//EN\\nCALSCALE:GREGORIAN\\nMETHOD:PUBLISH\\nX-WR-CALNAME:"+name+"\\nX-WR-TIMEZONE:"+TZID+"\\n"+VTIMEZONE+"\\n"+events.join("\\n")+"\\nEND:VCALENDAR\\n";
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
function renderCats(){{
  const bar = document.getElementById("catBar");
  bar.innerHTML = "";
  CATS.forEach(c => {{
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = c.category + " · " + c.games;
    if (c.category === cat) b.classList.add("is-on");
    b.onclick = () => {{ cat = c.category; selectedDate = null; renderAll(); }};
    bar.appendChild(b);
  }});
  const meta = CATS.find(c => c.category === cat);
  const dl = document.getElementById("dlAll");
  if (meta) {{
    dl.href = meta.file;
    dl.download = meta.slug + ".ics";
    dl.textContent = "Download season · " + meta.category;
  }}
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
  for (let i=0;i<startPad;i++) {{
    const cell = document.createElement("div");
    cell.className = "cal-cell mute";
    grid.appendChild(cell);
  }}
  for (let day=1; day<=daysInMonth; day++) {{
    const d = new Date(view.getFullYear(), view.getMonth(), day);
    const key = ymd(d);
    const list = byDate[key] || [];
    const cell = document.createElement("div");
    cell.className = "cal-cell" + (list.length ? " has" : "") + (selectedDate===key ? " is-sel" : "");
    let html = '<div class="d">'+day+'</div>';
    if (list.length) {{
      html += '<div class="glist">' + list.map(g =>
        '<div class="g"><span class="t">'+escapeHtml(g.time||"TBD")+'</span>vs '+escapeHtml(shortOpp(g.opponent))+'</div>'
      ).join("") + '</div>';
      cell.onclick = () => {{ selectedDate = key; renderAll(); }};
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
      downloadIcs(slugFor(cat) + "-game-" + g.id + ".ics", icsFile([g], cat));
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
        hero_lead="Games on the grid. Download one game, one day, or the full season.",
    )



def render_all(
    snapshot_path: Path,
    out_dir: Path,
    pages_base_url: str = "",
    today: str | None = None,
    last_success_path: Path | None = None,
    executed_at: datetime | None = None,
    cfg: dict[str, Any] | None = None,
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
    sat, sun = next_weekend(d_today)
    weekend_n = sum(1 for g in games if g.get("date") in (sat.isoformat(), sun.isoformat()))
    clash_cats = _clash_categories(games)
    ics_index = write_category_ics(games, out_dir / "ics")
    club = club_info()

    (out_dir / "index.html").write_text(
        build_index(
            snapshot,
            pages_base_url,
            last_run=last_run,
            next_run=next_run,
            weekend_n=weekend_n,
            clash_bands=clash_cats,
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
    meta = {
        "probedAt": snapshot.get("probedAt"),
        "gameCount": len(snapshot.get("games") or []),
        "clubId": club.get("id"),
        "clubName": club.get("name"),
        "timezone": schedule_timezone(),
        "weekend": {"sat": sat.isoformat(), "sun": sun.isoformat(), "games": weekend_n},
        "clashCategories": clash_cats,
        "calendarCategories": ics_index,
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
    )
    print(json.dumps(meta, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
