# Floorballbunnies schedule

Monday schedule watch, clash detection, Vienna ICS calendars, and coach digests for [Floorballbunnies](https://www.floorballbunnies.at) (FloorballFlash MCP).

**Full end-to-end documentation:** [`docs/e2e.md`](docs/e2e.md)

Public site (after Pages is enabled): [https://hamycz.github.io/floorballbunnies-schedule]

## Quick start

```bash
cd tools/schedule-watch
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python3 render_site.py --snapshot data/schedule-snapshot.json --out-dir pages
```

## Enable on GitHub

See [`docs/e2e.md` §9](docs/e2e.md#9-enable-on-github-production). 
Workflow file: [`.github/workflows/schedule-watch.yml`](.github/workflows/schedule-watch.yml).
