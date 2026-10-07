# Floorballbunnies schedule-watch — end-to-end documentation

**This is the only documentation file you need.**  
Everything about the Monday watch, change detection, site, email, fortress, enablement, local testing, and multi-club onboarding is below. Other files under `docs/` are previews or legacy notes; treat this file as authoritative.

| | |
|---|---|
| Club (default) | Floorballbunnies · FloorballFlash club id **78** |
| Tooling root | `tools/schedule-watch/` |
| Active config | `tools/schedule-watch/config.yaml` |
| Baseline | `tools/schedule-watch/data/schedule-snapshot.json` |
| Site preview | `docs/site/` (open HTML files locally — do **not** use agent `127.0.0.1` links) |
| MCP | `mcp.floorballflash.at` (HTTPS, no auth today) |
| Cron | Monday `07:00` UTC ≈ `08:00` Europe/Vienna (CET winter) |
| Timezone | `Europe/Vienna` (ICS + reminders) |

---

## Table of contents

1. [What this system does](#1-what-this-system-does)
2. [Two coach surfaces (email ≠ website)](#2-two-coach-surfaces-email--website)
3. [Monday end-to-end flow](#3-monday-end-to-end-flow)
4. [Change detection (watch list)](#4-change-detection-watch-list)
5. [Fortress gates and security](#5-fortress-gates-and-security)
6. [Squads, clashes, weekend, calendar, ICS](#6-squads-clashes-weekend-calendar-ics)
7. [Config reference](#7-config-reference)
8. [File layout](#8-file-layout)
9. [Enable on GitHub (production)](#9-enable-on-github-production)
10. [Local commands and testing](#10-local-commands-and-testing)
11. [Multi-club (other Austrian teams)](#11-multi-club-other-austrian-teams)
12. [Secrets, variables, permissions](#12-secrets-variables-permissions)
13. [Coach routing / split digests](#13-coach-routing--split-digests)
14. [All-clear heartbeat](#14-all-clear-heartbeat)
15. [Category audit (Alpencup + adults)](#15-category-audit-alpencup--adults)
16. [Pre-go-live verification log](#16-pre-go-live-verification-log)
17. [How to extend](#17-how-to-extend)

---

## 1. What this system does

Once a week (Monday), GitHub Actions:

1. Fetches the club’s **league** schedule from FloorballFlash MCP.
2. Runs **fortress health gates** (schema, count-drop, max churn).
3. **Diffs** the new snapshot against the committed baseline (add / remove / field changes).
4. Rebuilds the **branded static site** (weekend, clashes, calendar + ICS).
5. Optionally emails coaches a **short digest** if something changed (Resend).
6. On a healthy run: commits updated snapshot + pages, deploys GitHub Pages.

It does **not** email the full website. It does **not** alert on score-only updates.

---

## 2. Two coach surfaces (email ≠ website)

| Surface | What | What it is not |
|---|---|---|
| **GitHub Pages** | Full branded UI: home, weekend, clashes, calendar, `.ics` downloads | Not pasted into email |
| **Email digest** | Short change summary + counts + cancellations + links to Pages | Not the weekend timetable or clash accordion |

### Pages (website)

Built into `tools/schedule-watch/pages/` and mirrored for preview under `docs/site/`:

| File | Content |
|---|---|
| `index.html` | Hub — stats, last/next run, shortcuts |
| `weekend.html` | Next Sat–Sun club games (or empty state) |
| `clashes.html` | Same-squad SAME DAY / TIME CLASH view |
| `calendar.html` | Month grid + per-game / per-day / season ICS download |
| `ics/<slug>.ics` | One calendar file per squad/category |

Theme: dark `#0d0d0d` + lime `#ccff00`, club crest, Oswald/Barlow — aligned with floorballbunnies.at.  
Generated HTML includes a Content-Security-Policy when `fortress.csp: true`.

### Email digest

Produced as `out/changes.md` + `out/changes.html` (email-safe tables + inline CSS).

| Included | Not included |
|---|---|
| Counts (changed / added / removed) | Full weekend list |
| Per-game field diffs | Full clash UI |
| Cancellation highlights | Site chrome / nav |
| Links to `PAGES_BASE_URL` | Huge HTML dump of Pages |

**Sent when:**

| Condition | Email |
|---|---|
| Healthy + `has_changes=true` + Resend | Change digests (optionally **split by `competitionId`** — see [§13](#13-coach-routing--split-digests)) |
| Healthy + `has_changes=false` + Resend | **All-clear heartbeat** to `HEARTBEAT_TO` / `ALERT_TO` — see [§14](#14-all-clear-heartbeat) |

Implementation: `send_digests.py` (dry-run writes `out/digests/`; workflow passes `--send`).

### Previewing without a live Pages URL

Open files under `docs/site/` in a browser (file:// or your own machine).  
Agent chat links like `http://127.0.0.1:…` fail on your laptop with Error **-102** — that port is only on the remote agent VM.

---

## 3. Monday end-to-end flow

```
FloorballFlash MCP (mcp.floorballflash.at)
        │
        ▼
 fetch_schedule.py
   · clubId + nameMatchers from config
   · retry / backoff / fetchMetrics
   · write schedule-snapshot-new.json
        │
        ▼
 fortress gates (run_watch.py)
   · validate_snapshot (schema + clubId)
   · count-drop  (< minGameRatio of previous game count)
   · max added / removed churn
        │
        ▼
 compare_snapshots.py
   · watchFields from config / snapshot
   · out/changes.{json,md,html}
        │
        ├── render_site.py → pages/*.html + ics/*  (every healthy run)
        ├── Resend email   → only if has_changes + secrets
        ├── git commit     → snapshot + pages + last-success
        └── Pages deploy   → artifact from THIS run (not stale checkout)
```

| Step | Healthy | Unhealthy |
|---|---|---|
| Fetch | New snapshot JSON | Job fails → Issue + optional heartbeat email |
| Schema / count-drop / churn | Continue | Snapshot **frozen**; Issue + optional email; job fails |
| Diff | `has_changes` true/false | — |
| Site render | Always on healthy run | Skipped |
| Email coaches | Only if changes + Resend | Skipped |
| Commit + Pages | Snapshot + pages + last-success | No commit / no deploy |

### Baseline lifecycle

| Step | What happens |
|---|---|
| **First seed** | Live MCP fetch written to `data/schedule-snapshot.json` (committed once) |
| **Weekly refresh** | Healthy run diffs, then replaces baseline (`--always-update-snapshot` in workflow) |
| **Bad fetch** | Fortress refuses update; baseline stays; alert |
| **Companion** | `data/last-success.json` = last healthy `probedAt` + `gameCount` |

Do **not** hand-edit the baseline for routine updates.

---

## 4. Change detection (watch list)

Comparison is by Flash game **`id`**, across the **whole club** (all competitions).  
There is **no separate per-category email**. Competition moves still show up because `competitionId` / `competitionName` are watched.

### Outcomes per game id

| Situation | Result |
|---|---|
| `id` only in new snapshot | **Added** |
| `id` only in baseline | **Removed** |
| `id` in both, any **watched** field differs | **Changed** (field-by-field from → to) |
| `id` in both, only non-watched fields differ | **Ignored** |

`hasChanges = true` if any added, removed, or changed game exists.

### Watched fields (exact list)

Must match `config.yaml` → `schedule.watchFields`:

| # | Field | Meaning | Example that triggers |
|---|---|---|---|
| 1 | `date` | Match date `YYYY-MM-DD` | `2026-11-15` → `2026-11-16` |
| 2 | `time` | Kickoff `HH:MM` (Vienna wall clock as Flash publishes) | `19:00` → `20:30` |
| 3 | `venue` | Location / hall | `Alterlaa` → `Kagran` |
| 4 | `state` | Fixture state | `BeforeGame` → cancelled / postponed |
| 5 | `homeTeam` | Home team label | Team rename / swap |
| 6 | `awayTeam` | Away team label | Team rename / swap |
| 7 | `competitionId` | Flash competition id | Moved to another competition |
| 8 | `competitionName` | Competition display name | League rename / move |

**Comparison rules**

- Trimmed string compare; `null` / missing ≡ `""`.
- Any watched-field difference → game is **changed**.
- Multiple field changes on one game are listed in one entry.

**Cancellation highlight**

If `state` changes into a cancel/postpone-like value, the game is also listed under **cancellations**.  
Hints matched case-insensitively inside `state`: `cancel`, `abandon`, `postpon`, `walkover`, `forfeit`.

### Every game field — watched or not

| Field | Watched? | Notes |
|---|---|---|
| `id` | Identity key | Match key for add/remove/change |
| `date` | **Yes** | |
| `time` | **Yes** | May be missing (TBD) |
| `venue` | **Yes** | Location |
| `state` | **Yes** | Also drives cancellation list |
| `homeTeam` | **Yes** | |
| `awayTeam` | **Yes** | |
| `competitionId` | **Yes** | |
| `competitionName` | **Yes** | |
| `homeAway` | No | Derived from club name matchers |
| `opponent` | No | Derived; follows team fields |
| `clubTeamName` | No | Club side label when detected |
| `homeScore` | No | Score updates must not email coaches |
| `awayScore` | No | Score updates must not email coaches |

### Explicitly not detected

| Change | Detected? |
|---|---|
| Score-only updates | No |
| Derived opponent/homeAway alone | No |
| Per-category digest email | No (club-wide digest; lines include competition name) |
| Site “category” remapping without Flash competition change | No |

### How to change the watch list

1. Edit `schedule.watchFields` in `config.yaml` (and club profiles / `_template.yaml`).
2. Ensure `fetch_schedule.py` stores that field on each game.
3. Update **this section** of this file so it stays exact.
4. Next healthy fetch embeds `watchFields` into the snapshot; diff prefers snapshot → config → code defaults.

---

## 5. Fortress gates and security

Treat the pipeline as privileged: it can commit to the default branch and publish Pages.

### Before enabling the workflow — protect `main`

GitHub → **Settings → Rules → Rulesets** (or classic branch protection) on `main`:

| Setting | Recommended |
|---|---|
| Block force-push / deletion | On |
| Require a pull request before merging | Optional (if On, you **must** bypass Actions — see below) |
| Allow GitHub Actions to push | **Required** — the watch job commits updated snapshot + pages |

**If the `Commit snapshot + pages` step fails** while fetch is healthy: the ruleset is blocking `github-actions[bot]`.

Fix (pick one):
1. Ruleset → **Bypass list** → add **App → GitHub Actions** (search under Apps / Integrations), or
2. Create a fine-grained PAT (Contents: Read and write on this repo), add as secret **`SCHEDULE_PUSH_TOKEN`**, and add that account to the ruleset bypass list.

Until `main` is protected, **do not** enable the workflow that has `contents: write`.

### Gates already implemented

| Gate | Behavior |
|---|---|
| Config as source of truth | Club, MCP, watch fields, brand, reminders, fortress knobs |
| Snapshot schema | Requires `schemaVersion`, `clubId`, `games[]` with `id`/`date`/`competitionName`; rejects empty, bad dates, duplicate ids, club mismatch |
| Count-drop | New count &lt; `minGameRatio` × old (default **0.70**) → refuse update |
| Max churn | Added &gt; `maxAddedGames` or removed &gt; `maxRemovedGames` (default **100**) → refuse |
| HTTPS Pages URL | `PAGES_BASE_URL` must be `https://…` when `requireHttpsPagesUrl: true` |
| URL sanitize | Reject `javascript:`, quotes, whitespace, `<>` |
| CSP | Tight policy on generated HTML |
| Calendar XSS | `escapeHtml` on Flash strings in calendar JS |
| Least-privilege Actions | See [§12](#12-secrets-variables-permissions) |
| Artifact Pages deploy | Deploy artifact from **this** run, not trigger-SHA checkout |
| Safe `GITHUB_OUTPUT` | Multiline delimiter form |
| ICS Vienna TZ | `TZID=Europe/Vienna` + `VTIMEZONE` (not floating) |
| Secrets | Resend only via GitHub Secrets — never in repo |

Tune under `config.yaml` → `fortress:`.

---

## 6. Squads, clashes, weekend, calendar, ICS

### Age band vs squad/category

| Layer | Examples | Used for |
|---|---|---|
| Age band | U8…U17, Adults | Coarse grouping |
| **Squad / category** | U12 Assist, U12 Bully, U12 Mädchen, U14 Großfeld, Adults Grossfeld, Bundesliga, … | **Clashes** + **calendar ICS** |

Categories stay **separate calendars/ICS** (order in `render_site.py`):

`U8`, `U10`, `U10 Alpencup`, `U12 Assist`, `U12 Bully`, `U12 Mädchen`, `U12 Alpencup`, `U14 Großfeld`, `U14 Kleinfeld`, `U14w`, `U17`, `U17w`, `Adults Grossfeld`, `Adults Kleinfeld`, `Bundesliga`

**Confirmed club meaning**

| Category | Role |
|---|---|
| **U12 Bully** | A team — own calendar |
| **U12 Assist** | B team — own calendar |
| **U12 Alpencup** | Separate (OÖ) — own calendar |
| **U10 Alpencup** | Separate (OÖ) — own calendar |

### Clash rules

| Detect | Meaning |
|---|---|
| **SAME DAY** (same squad) | Same category has games in **2+ different competitions** on one calendar day |
| **TIME CLASH** (same squad) | Same category, different competitions, kickoffs overlap within **~90 minutes** — **not used for U12** |
| **U12 same day** | Any **2+** of Assist / Bully / Mädchen / Alpencup play that day (informational) |
| **Not a clash** | Unrelated age bands (e.g. U12 vs U17); U12 kickoff time overlap is intentionally ignored |

U12 Assist, Bully, Mädchen, and Alpencup stay **own categories/calendars**. Overlaps between **any two** are listed under **U12 group overlap** as **same-day only** (no time-overlap check).

Site page: `pages/clashes.html` / preview `docs/site/clashes.html`.

### Weekend

Next Saturday–Sunday club games (upcoming only; finished filtered out on the site).  
Page: `weekend.html`.

### Calendar + ICS

| Action | Result |
|---|---|
| Pick category | Month grid (current month); kickoffs in cells on desktop |
| Mobile | Compact day cells (date + game count); details in the day panel |
| Click day | Day panel: download that day / that game |
| Download season | Full category `.ics` |

**Timezone:** every timed event uses `DTSTART;TZID=Europe/Vienna:…` plus a full `VTIMEZONE` (CET/CEST). Also `X-WR-TIMEZONE:Europe/Vienna`. Not floating local times.

**ICS compatibility (RFC 5545):** files use **CRLF** line endings, **`DTSTAMP`** on every `VEVENT`, line folding for long `DESCRIPTION`s, and exclusive `DTEND` for all-day (`VALUE=DATE`) events. Browser “download day/game” builds the same rules client-side.

**Adult reminders** (config → `reminders`; defaults):

Categories: `Bundesliga`, `Adults Grossfeld`, `Adults Kleinfeld`.

For each game in those categories, three reminder events at **09:00 Vienna** on the Monday / Wednesday / Friday **strictly before** the game day (with display `VALARM`). Youth categories do not get these.

Import: download `.ics` into Google Calendar / Apple / Outlook, or later subscribe to `{PAGES_BASE_URL}/ics/<slug>.ics`.

---

## 7. Config reference

File: `tools/schedule-watch/config.yaml` (active Floorballbunnies profile).

```yaml
schemaVersion: 1

club:
  id: 78
  name: Floorballbunnies
  slug: floorballbunnies
  url: https://www.floorballbunnies.at
  logoUrl: https://www.floorballbunnies.at/wp-content/uploads/2021/01/cropped-bunnies_wappen_2020-192x192.png
  nameMatchers:
    - Floorballbunnies
    - Floorballbunnies Wien

mcp:
  host: mcp.floorballflash.at
  timeoutSeconds: 180
  maxAttempts: 20

schedule:
  cron: "0 7 * * 1"          # documented intent; workflow YAML holds the real cron
  timezone: Europe/Vienna
  watchFields:               # see §4
    - date
    - time
    - venue
    - state
    - homeTeam
    - awayTeam
    - competitionId
    - competitionName

fortress:
  minGameRatio: 0.70
  maxAddedGames: 100
  maxRemovedGames: 100
  requireHttpsPagesUrl: true
  validateSnapshot: true
  csp: true

brand:
  lime: "#ccff00"
  ink: "#0d0d0d"

reminders:
  categories:
    - Bundesliga
    - Adults Grossfeld
    - Adults Kleinfeld
  weekdays: [mon, wed, fri]
  hour: 9
```

Env overrides: `SCHEDULE_WATCH_CONFIG`, `CLUB_ID`, `CLUB_NAME`, `MCP_HOST`, `PAGES_BASE_URL`.

Loader: `config_loader.py` (deep-merge with defaults).

---

## 8. File layout

```
tools/schedule-watch/
  config.yaml                 Active club profile
  clubs/_template.yaml        Template for other clubs
  clubs/<slug>.yaml           Optional per-club profiles
  config_loader.py            Load / merge config
  validate_snapshot.py        Schema gate
  fetch_schedule.py           MCP fetch + retries + metrics
  compare_snapshots.py        Diff + markdown + email HTML
  render_site.py              Pages + ICS + CSP + brand
  run_watch.py                One full cycle
  send_digests.py             Split digests + all-clear heartbeat
  requirements.txt            pyyaml
  local-test.sh               Optional local static server
  data/
    schedule-snapshot.json    Baseline
    last-success.json         Last healthy probe marker
  pages/                      Published static site (+ ics/)
  github-actions/
    schedule-watch.yml        Cron + Issues + Resend + Pages
  out/                        CI/local artifacts (not the product)
  out/digests/                Email previews / send manifest

docs/
  e2e.md                      ← THIS FILE (full documentation)
  site/                       Preview copy of pages (+ sample email files)
```

---

## 9. Enable on GitHub (production)

Do these in order:

1. **Protect `main`** (required reviews, no force-push) — see [§5](#5-fortress-gates-and-security).
2. Copy `tools/schedule-watch/` into the club repo (include `data/`, `pages/`, `config.yaml`, scripts).
3. Copy `github-actions/schedule-watch.yml` → `.github/workflows/schedule-watch.yml`.
4. Repo **Settings → Pages → Source: GitHub Actions**.
5. Run workflow once via **workflow_dispatch**. Confirm the public Pages URL.
6. Set repo variable **`PAGES_BASE_URL`** to that URL (`https://…`, no trailing slash required).
7. Optional: Resend secrets (`RESEND_API_KEY`, `ALERT_TO`, `ALERT_FROM`, `HEARTBEAT_TO`).
8. Optional: `SCHEDULE_WATCH_CONFIG` if not using default `config.yaml`.

Create Issues labels only if you want them; the workflow creates Issues without requiring labels.

---

## 10. Local commands and testing

```bash
cd tools/schedule-watch
pip install -r requirements.txt   # or pip3 / python3 -m pip

# Rebuild site from committed baseline
python3 render_site.py \
  --snapshot data/schedule-snapshot.json \
  --out-dir pages \
  --pages-base-url 'https://USER.github.io/REPO' \
  --today 2026-10-05

# Full cycle without live MCP (treat a JSON file as the "new" fetch)
python3 run_watch.py \
  --new-file data/schedule-snapshot.json \
  --out-dir out \
  --pages-dir pages \
  --pages-base-url 'https://USER.github.io/REPO' \
  --always-update-snapshot

# Diff only → email digest HTML
python3 compare_snapshots.py \
  --old data/schedule-snapshot.json \
  --new out/schedule-snapshot-new.json \
  --out-html out/changes.html \
  --out-md out/changes.md \
  --pages-base-url 'https://USER.github.io/REPO'

# Live fetch (needs network to mcp.floorballflash.at)
python3 fetch_schedule.py --out out/live.json
```

Open `pages/*.html` or `docs/site/*.html` in a browser on **your** machine.

To simulate a change: copy the baseline, edit one watched field (e.g. `time` / `venue`) on one game, run `run_watch.py --new-file …` and inspect `out/changes.json`.

---

## 11. Multi-club (other Austrian teams)

Same pipeline; swap the profile YAML + seed a new baseline.

### Profile layout

| Path | Role |
|---|---|
| `config.yaml` | Active profile (Bunnies today) |
| `clubs/_template.yaml` | Blank template |
| `clubs/<slug>.yaml` | One file per club |

```bash
export SCHEDULE_WATCH_CONFIG=clubs/<slug>.yaml
# or: python3 run_watch.py --config clubs/<slug>.yaml …
```

### Onboard checklist

1. Find FloorballFlash `clubId`.
2. `cp clubs/_template.yaml clubs/<slug>.yaml` — fill `club.*` + `nameMatchers`.
3. Set `reminders.categories` for that club’s adult labels (or `[]`).
4. Optional brand colors / logo.
5. Seed baseline:
   ```bash
   SCHEDULE_WATCH_CONFIG=clubs/<slug>.yaml \
     python3 fetch_schedule.py --out data/schedule-snapshot.json --club-id <id>
   ```
6. Render once; open `pages/index.html`.
7. Protect `main`, copy tooling + workflow, set `PAGES_BASE_URL`, optional Resend.
8. If Flash competition names don’t match Bunnies squad mapping, extend `category_of()` in `render_site.py`.

### Shared vs per-club

| Shared | Per-club |
|---|---|
| MCP host, fortress gates, Vienna TZ | `club.*`, name matchers |
| Clash rule (same squad) | Baseline snapshot |
| Email = digest / Pages = full site | Reminder categories, brand, secrets, Pages URL |

Scaling: prefer **one GitHub repo per club** unless you already run a federation monorepo.

---

## 12. Secrets, variables, permissions

### Repo variables

| Name | Required | Purpose |
|---|---|---|
| `PAGES_BASE_URL` | For useful email links | Public Pages root, `https://…` |
| `SCHEDULE_WATCH_CONFIG` | No | Path to club YAML if not default |
| `CLUB_ID` / `CLUB_NAME` | No | Env overrides |

### Secrets

| Name | Required | Purpose |
|---|---|---|
| `RESEND_API_KEY` | No | Send mail |
| `ALERT_TO` | No | Coach digest recipients (comma-separated) |
| `ALERT_FROM` | No | From address |
| `HEARTBEAT_TO` | No | Failure alerts **and** weekly all-clear (falls back to `ALERT_TO`) |
| `SCHEDULE_PUSH_TOKEN` | No | Fine-grained PAT to push baseline commits when the ruleset blocks `GITHUB_TOKEN` |

No secrets → still fetch, diff, Issues on failure, deploy Pages. Emails skipped. Baseline git push needs either a ruleset bypass for GitHub Actions or `SCHEDULE_PUSH_TOKEN`.

### Workflow permissions

```
workflow default: contents: read

watch job:      contents: write, issues: write, actions: write
heartbeat job:  issues: write
pages job:      pages: write, id-token: write
```

---

## 13. Coach routing / split digests

`config.yaml` → `coaches:` maps **Flash `competitionId` → email list**.

| Behavior | Detail |
|---|---|
| Mapped competition changes | Only those coaches get that slice of the digest |
| Unmapped competition changes | Fall back to secret `ALERT_TO` |
| No coaches map / all empty lists | Single full digest to `ALERT_TO` |
| `coachesNotifyDefaultFull: true` | Also send full club digest to `ALERT_TO` |

```bash
# Dry-run (writes out/digests/*.html)
ALERT_TO='fallback@example.com' python3 send_digests.py \
  --mode changes --changes-json out/changes.json --out-dir out/digests

# Deliver (workflow does this with --send)
ALERT_TO=… RESEND_API_KEY=… python3 send_digests.py --mode changes --send
```

Fill real addresses in `config.yaml` before go-live (lists are empty placeholders today).  
Preview example: `docs/site/digests/coach-split-example.html`.

---

## 14. All-clear heartbeat

On a **healthy** Monday with **no** schedule changes, the workflow sends a short “schedule watch OK — no changes” mail to `HEARTBEAT_TO` (else `ALERT_TO`).

Purpose: peace of mind that the job ran, not silence-from-failure.

```bash
HEARTBEAT_TO='ops@example.com' python3 send_digests.py \
  --mode all-clear --health-json out/health.json --out-dir out/digests
```

Preview: `docs/site/digests/all-clear.html`.

---

## 15. Category audit (Alpencup + adults)

Audited against club 78 / season 2026 snapshot (200 games).

### U12 / U10 Alpencup

| Flash competition | id | Mapped category | Notes |
|---|---|---|---|
| `OÖ U12 + Alpencup` | 750 | **U12 Alpencup** | Own squad — **not** Assist, **not** Bully |
| `Wiener U12 Assist Meisterschaft` | 724 | U12 Assist | Separate |
| `Wiener U12 Bully Meisterschaft` | 723 | U12 Bully | Separate |
| `OÖ U10 + Alpencup` | 749 | **U10 Alpencup** | Own calendar |

**Confirmed:** Alpencup stays **separate** (own calendar + own ICS). Do not merge into Assist/Bully.  
**Confirmed:** Assist, Bully, Mädchen, and Alpencup stay **own categories**, with **U12 group overlap** alerts when any two play the **same day** (no kickoff time-overlap check).

### Adult reminder categories

| Reminder category (config) | Flash competition name | id |
|---|---|---|
| `Bundesliga` | `ÖFBV Bundesliga Herren` | 737 |
| `Adults Grossfeld` | `Wiener Grossfeldliga` | 731 |
| `Adults Kleinfeld` | `Wiener Kleinfeldliga` | 730 |

These match how FloorballFlash labels the three adult leagues for club 78. ICS reminders (Mon/Wed/Fri 09:00 Vienna) attach only to these three.

---

## 16. Pre-go-live verification log

| Check | Result | When |
|---|---|---|
| Live MCP fetch | **OK** — 200 games, 0 retries, `probedAt=2026-10-06T22:04:51Z` | 2026-10-06 |
| Live vs previous baseline | **0 changes** (schedule stable); baseline refreshed to live fetch | 2026-10-06 |
| Simulate time+venue change on game `#16094` | **hasChanges=true**, 1 field-change digest | 2026-10-06 |
| Split digest by `competitionId` 725 | Routed to coach bucket `coach-725` only | 2026-10-06 |
| All-clear heartbeat dry-run | Wrote `out/digests/all-clear.*` | 2026-10-06 |
| MCP session-less init | Client updated (host no longer returns `Mcp-Session-Id`) | 2026-10-06 |

Artifacts: `tools/schedule-watch/out/live-fetch.json`, `out/digests/`, `docs/site/email-digest.html`, `docs/site/digests/`.

---

## 17. How to extend

| Goal | Where |
|---|---|
| Watch another logistics field | `schedule.watchFields` + store field in `fetch_schedule.py` + update §4 here |
| New adult reminder categories | `reminders.categories` |
| Other club | `clubs/<slug>.yaml` + §11 |
| Real coach emails | `coaches:` lists in `config.yaml` |
| Tighter fortress | Lower `minGameRatio` / `maxAddedGames` / `maxRemovedGames` |
| Deploy key instead of `GITHUB_TOKEN` write | Optional hardening — replace commit step credentials |

---

## Quick operator checklist

- [x] Live MCP fetch once (refreshed baseline)
- [x] Simulate field change → digest + `hasChanges`
- [x] Alpencup / adult category audit documented (§15) — Alpencup **confirmed separate**
- [x] Coach split digests + all-clear heartbeat implemented
- [ ] Put real emails into `config.yaml` → `coaches:`
- [ ] Read §4 (watch list) and §5 (protect `main`)
- [ ] Tooling + workflow in club repo
- [ ] Branch protection on `main`
- [ ] Pages = GitHub Actions; `PAGES_BASE_URL` set (https)
- [ ] Resend secrets (`RESEND_API_KEY`, `ALERT_TO`, `ALERT_FROM`, `HEARTBEAT_TO`)
- [ ] One manual `workflow_dispatch` succeeds
- [ ] Open Pages URL; confirm weekend / clashes / calendar / ICS
