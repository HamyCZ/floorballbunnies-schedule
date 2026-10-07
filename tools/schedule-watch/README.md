# schedule-watch

**Full end-to-end documentation (single file):**

→ [`../../docs/e2e.md`](../../docs/e2e.md)

| Path | Role |
|---|---|
| `config.yaml` | Active club profile |
| `clubs/_template.yaml` | Other-club template |
| `run_watch.py` | One weekly cycle |
| `send_digests.py` | Split coach digests + all-clear heartbeat |
| `fetch_schedule.py` / `compare_snapshots.py` / `render_site.py` | Fetch · diff · site |
| `data/schedule-snapshot.json` | Baseline |
| `pages/` | Static site + `ics/` |
| `github-actions/schedule-watch.yml` | Cron + Pages deploy |
