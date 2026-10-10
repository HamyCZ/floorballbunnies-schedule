# Floorballbunnies schedule

Monday schedule watch, clash detection, Vienna ICS calendars, and coach digests for <a href="https://www.floorballbunnies.at" target="_blank" rel="noopener noreferrer">Floorballbunnies</a> (<a href="https://mcp.floorballflash.at" target="_blank" rel="noopener noreferrer">FloorballFlash MCP</a>).

Public site: <a href="https://hamycz.github.io/floorballbunnies-schedule" target="_blank" rel="noopener noreferrer">https://hamycz.github.io/floorballbunnies-schedule</a>

## Contents

- [Quick start](#quick-start)
- [What it does](#what-it-does)
- [Monday flow](#monday-flow)
- [Change detection](#change-detection)
  - [Watched fields and ignores](#watched-fields-and-ignores)
  - [Changes page](#changes-page)
- [Fortress and main protection](#fortress-and-main-protection)
- [Squads, clashes, calendar, ICS](#squads-clashes-calendar-ics)
- [Config](#config)
- [Secrets and variables](#secrets-and-variables)

| | |
|---|---|
| Club (default) | Floorballbunnies · FloorballFlash club id **78** |
| Tooling | `tools/schedule-watch/` |
| Config | `tools/schedule-watch/config.yaml` |
| Baseline | `tools/schedule-watch/data/schedule-snapshot.json` |
| Site preview | `docs/site/` (open locally) · built pages in `tools/schedule-watch/pages/` |
| MCP | `mcp.floorballflash.at` |
| Cron | Monday `07:00` UTC ≈ `08:00` Europe/Vienna |
| Workflow | [`.github/workflows/schedule-watch.yml`](.github/workflows/schedule-watch.yml) |

---

## Quick start

```bash
cd tools/schedule-watch
python3 -m venv .venv
# macOS/Linux: source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
python3 render_site.py --snapshot data/schedule-snapshot.json --out-dir pages
```

Open `docs/site/` or `tools/schedule-watch/pages/` in a browser (file://).

### Tooling layout

| Path | Role |
|---|---|
| `config.yaml` | Active club profile |
| `clubs/_template.yaml` | Other-club template |
| `run_watch.py` | One weekly cycle |
| `send_digests.py` / `mailer.py` | Digests + SMTP (Resend optional fallback) |
| `fetch_schedule.py` / `compare_snapshots.py` / `render_site.py` | Fetch · diff · site |
| `data/schedule-snapshot.json` | Baseline |
| `pages/` | Static site + `ics/` |
| `github-actions/schedule-watch.yml` | Packaged workflow copy |

---

## What it does

Once a week (Monday), GitHub Actions:

1. Fetches the club’s **league** schedule from FloorballFlash MCP.
2. Runs **fortress health gates** (schema, count-drop, max churn).
3. **Diffs** the new snapshot against the committed baseline.
4. Rebuilds the **branded static site** (weekend, clashes, calendar, changes + ICS).
5. Optionally emails coaches a **short digest** via **SMTP** if something changed (`RESEND_API_KEY` is an optional fallback only).
6. On a healthy run: commits updated snapshot + pages, then the separate **pages** job deploys GitHub Pages.

It does **not** email the full website. It does **not** alert on score-only updates.

### Email ≠ website

| Surface | What |
|---|---|
| **GitHub Pages** | Full UI: home, this week, clashes, calendar, changes, `.ics` downloads |
| **Email digest** | Short change summary + links to Pages (not the full site) |

| Condition | Email |
|---|---|
| Healthy + changes + SMTP ready | Change digests (optionally split by `competitionId`) |
| Healthy + no changes + SMTP ready | All-clear heartbeat to `HEARTBEAT_TO` / `ALERT_TO` (step inside the `watch` job) |
| Unhealthy / job failure | Issue + optional alert; `heartbeat-on-failure` job runs only on failure |

Mail is ready when `config.yaml` → `mail.smtp.host` (or env `SMTP_HOST`) and secret `SMTP_PASSWORD` are set. `RESEND_API_KEY` is optional fallback only.

---

## Monday flow

```
FloorballFlash MCP → fetch → fortress gates → diff → render site
  → SMTP digests (changes) or all-clear heartbeat (no changes)   ← steps in watch job
  → git commit snapshot + pages
  → pages job deploys GitHub Pages (separate job)
  → on failure only: heartbeat-on-failure (Issue + optional email)
```

| Step | Healthy | Unhealthy |
|---|---|---|
| Fetch / fortress | Continue | Snapshot frozen; Issue + optional email; job fails |
| Site render | Always | Skipped |
| Email | Digests or all-clear (needs config `mail.smtp` + secrets) | Failure alert path |
| Commit + Pages job | Yes | No |

Do **not** hand-edit the baseline for routine updates. Companion file: `data/last-success.json`.

**Note:** The `pages` job reads a remapped `health_ok` job output (`ok`/`fail`, not `true`/`false`) so GitHub secret scrubbing cannot blank the value and skip deploy after a healthy watch.

---

## Change detection

Each healthy Monday run diffs the new FloorballFlash snapshot against the committed baseline (`data/schedule-snapshot.json`) via `compare_snapshots.py`. Games are matched by Flash game `id` across the whole club.

### Watched fields and ignores

Watched fields (`config.yaml` → `schedule.watchFields`, defaults below):

| Watched | Meaning |
|---|---|
| `date`, `time` | Kickoff moved |
| `venue` | Hall / location changed |
| `state` | Status change (incl. cancel / postpone signals) |
| `homeTeam`, `awayTeam` | Side renamed or swapped in Flash |
| `competitionId`, `competitionName` | Competition reassignment |

**Counted as a change** (`hasChanges=true`):

- **Added** — game id present only in the new snapshot
- **Removed** — game id present only in the old snapshot
- **Field-changed** — any watched field value differs for the same id
- **Cancellations** — subset of field changes where `state` newly looks like cancel / postpone / abandon / walkover / forfeit (highlighted separately; still part of the field-change list)

**Ignored (not watched):**

- Score-only updates (`homeScore`, `awayScore`, …)
- Derived / display fields such as `opponent`, category labels, and anything else outside `watchFields`

Artifacts from the diff: `out/changes.json`, `out/changes.md`, and an email-safe `out/changes.html` (digest tables — not the public site).

Before the diff is accepted, **fortress health gates** can refuse the run (snapshot frozen, no site/email update): schema validation, empty fetch, count-drop (`minGameRatio`, default `0.70`), and max churn (`maxAddedGames` / `maxRemovedGames`). See [Fortress and main protection](#fortress-and-main-protection).

### Changes page

Public site page: [`changes.html`](https://hamycz.github.io/floorballbunnies-schedule/changes.html) (nav **Changes**).

- Same branded shell as Home / This week / Clashes / Changes / Calendar (nav, hero, footer, CSS) — not the email digest layout
- Shows the **last successful watch** report: summary counts, cancellations, field changes, new games, removed games, plus compared / previous / new probe timestamps
- **Empty state** when that run had no diffs: “No schedule changes since last check” (timestamps still shown when available)
- Durable copy: `pages/changes.json` is written on every healthy render so a later local rebuild can still populate the page
- Wired by `run_watch` → `render_site.py --changes-json out/changes.json`

---

## Fortress and main protection

Protect `main` **before** enabling the workflow (`contents: write` on the watch job):

- Block force-push / deletion
- Allow GitHub Actions to push (or set secret `SCHEDULE_PUSH_TOKEN`)

Gates (tune under `fortress:`): schema validation, empty fetch, count-drop (`minGameRatio` default 0.70), max churn (`maxAddedGames` / `maxRemovedGames`), HTTPS `PAGES_BASE_URL`, CSP on HTML, Vienna ICS TZ.

---

## Squads, clashes, calendar, ICS

Clashes and calendars use **squad/category** (U12 Assist, U12 Bully, Alpencup, Adults Grossfeld, …), not just age band.

- **SAME DAY clash:** same category in 2+ competitions on one day
- **U12 group overlap:** any 2+ of Assist / Bully / Mädchen / Alpencup that day
- Subscribe: `{PAGES_BASE_URL}/ics/<slug>.ics` (Vienna `TZID`, no `VALARM`)

Alpencup stays **separate** calendars (not merged into Assist/Bully). Adult leagues map to Bundesliga / Adults Grossfeld / Adults Kleinfeld.

---

## Config

Active profile: `tools/schedule-watch/config.yaml`. Env overrides (also wired as repository variables in Actions): `SCHEDULE_WATCH_CONFIG`, `CLUB_ID`, `CLUB_NAME`, `PAGES_BASE_URL`. Locally you can also set `MCP_HOST`.

### SMTP (`mail.smtp`)

Non-secret connection settings live in config (not GitHub Variables). Password, login user, and from/to addresses stay in Secrets / env only — never put a mailbox password in YAML.

```yaml
mail:
  smtp:
    host: mailserver.weinz.org
    port: 465
    ssl: true
```

Optional env overrides (if set): `SMTP_HOST`, `SMTP_PORT`, `SMTP_SSL`. Prefer editing `config.yaml` for this deployment.

### Coach emails (`coaches:`)

Optional map of Flash `competitionId` → email list for split digests. Unmapped changes go to secret `ALERT_TO`. Lists in config are empty placeholders today — put real addresses before go-live.

```yaml
coaches:
  737: []   # Bundesliga
  731: []   # Adults Grossfeld
  # …
coachesNotifyDefaultFull: false  # true → also send full digest to ALERT_TO
```

### Multi-club

Copy `clubs/_template.yaml` → `clubs/<slug>.yaml`, seed a baseline with `fetch_schedule.py`, set repository variable `SCHEDULE_WATCH_CONFIG` (or env locally). Prefer one GitHub repo per club.

---

## Secrets and variables

### Where to configure

GitHub → **Settings → Secrets and variables → Actions**

| Tab | Use |
|---|---|
| **Secrets → Repository secrets** | Mailbox password, mail addresses, optional Resend / push token |
| **Variables → Repository variables** | Pages URL, optional club overrides |

Never commit credentials. Never put passwords or mailbox addresses in Variables, Pages, or workflow logs. Keep `ALERT_*` / `HEARTBEAT_*` / `SMTP_USER` as **Secrets** (private).

### Repository secrets (keep private)

| Name | Required? | Purpose |
|---|---|---|
| `SMTP_USER` | No | Login username; falls back to `ALERT_FROM` |
| `SMTP_PASSWORD` | Yes (to send mail) | Mailbox or app password |
| `ALERT_FROM` | Yes (to send) | From address allowed by your provider |
| `ALERT_TO` | For digests / fallback | Coach digest recipients; also heartbeat fallback |
| `HEARTBEAT_TO` | No | Failure alerts + weekly all-clear (falls back to `ALERT_TO`) |
| `RESEND_API_KEY` | No | Optional fallback only if SMTP is not configured |
| `SCHEDULE_PUSH_TOKEN` | No | PAT with Contents:write if ruleset blocks `GITHUB_TOKEN` push |

Minimum for email: `config.yaml` → `mail.smtp` (host/port/ssl) and secrets `SMTP_PASSWORD`, `ALERT_FROM`, `ALERT_TO` (plus `HEARTBEAT_TO` if you want a separate ops inbox).

No mail transport configured → fetch, diff, Issues, and Pages still run; emails are skipped.

### Repository variables

Configured under **Variables** (not Secrets):

| Name | Required? | Example / used for |
|---|---|---|
| `PAGES_BASE_URL` | Recommended | Public Pages root (`https://…`) for digests, ICS links, fortress HTTPS check |
| `SCHEDULE_WATCH_CONFIG` | No | Path to alternate club YAML (default: `config.yaml`) |
| `CLUB_ID` | No | Override club id from config |
| `CLUB_NAME` | No | Override club name from config |

**Obsolete:** repository Variables `SMTP_HOST` / `SMTP_PORT` / `SMTP_SSL` are no longer used by the workflow (host/port/ssl come from `config.yaml`). You can delete those Variables if you added them earlier. Optional local/CI env overrides still work if set manually.

Leave club/Pages vars unset to use defaults from `tools/schedule-watch/config.yaml`.

### Workflow permissions

```
workflow default: contents: read
watch job:               contents: write, issues: write, actions: write
heartbeat-on-failure:    issues: write
pages job:               pages: write, id-token: write
```
