#!/usr/bin/env bash
# Run on YOUR machine after copying tools/schedule-watch/
set -euo pipefail
cd "$(dirname "$0")"
python3 render_site.py \
  --snapshot data/schedule-snapshot.json \
  --out-dir pages \
  --pages-base-url "${PAGES_BASE_URL:-https://example.github.io/bunnies-schedule}" \
  --today "${TODAY:-$(date +%F)}"
echo "Pages written to: $(pwd)/pages"
echo "Open: $(pwd)/pages/index.html"
echo "Calendar: $(pwd)/pages/calendar.html"
echo "Adult ICS sample: $(pwd)/pages/ics/adults-grossfeld.ics"
# optional: serve locally on YOUR laptop
if [[ "${SERVE:-}" == "1" ]]; then
  echo "Serving http://127.0.0.1:43145/  (Ctrl+C to stop)"
  python3 -m http.server 43145 --bind 127.0.0.1 --directory pages
fi
