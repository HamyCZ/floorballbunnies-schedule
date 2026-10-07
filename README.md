# Floorballbunnies schedule-watch

Monday schedule watch, clash detection, Vienna ICS calendars, and coach digests for Austrian floorball clubs (FloorballFlash MCP).

**Full end-to-end documentation:** [`docs/e2e.md`](docs/e2e.md)

## Quick start

```bash
cd tools/schedule-watch
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python3 render_site.py --snapshot data/schedule-snapshot.json --out-dir pages
```

See `docs/e2e.md` for fetch, fortress guards, GitHub Actions, and multi-club setup.
