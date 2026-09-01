#!/bin/bash
# Refresh cricket fixtures from ESPN and rebuild calendar.html.
#
#   ./run.sh              sync now, whatever the data's age  (what you run by hand)
#   ./run.sh --scheduled  sync only if the data is older than MAX_AGE_HOURS
set -uo pipefail
cd "$(dirname "$0")"

PY=/usr/bin/python3          # system python: no dependencies, never moves
MAX_AGE_HOURS=4
mkdir -p logs
LOG="logs/$(date +%Y-%m-%d).log"

if [ "${1:-}" = "--scheduled" ]; then
  AGE=$("$PY" - <<'PYEOF'
import datetime, json, pathlib, sys
f = pathlib.Path("data/fixtures.json")
if not f.exists():
    print(999); sys.exit()
try:
    g = json.loads(f.read_text())["generated_at"]
    t = datetime.datetime.strptime(g, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    print(round((datetime.datetime.now(datetime.timezone.utc) - t).total_seconds() / 3600, 2))
except Exception:
    print(999)
PYEOF
)
  if [ "$(echo "$AGE < $MAX_AGE_HOURS" | bc -l)" = "1" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S %Z')  skipped, data is ${AGE}h old" >> "$LOG"
    exit 0
  fi
fi

{
  echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z') ==="
  if "$PY" sync.py; then
    "$PY" build.py
  else
    echo "sync failed — calendar.html left as it was"
    exit 1
  fi
  echo
} 2>&1 | tee -a "$LOG"

find logs -name '*.log' -mtime +31 -delete 2>/dev/null || true
