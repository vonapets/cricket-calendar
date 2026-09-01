#!/usr/bin/env python3
"""
Inject data/fixtures.json into template.html and write calendar.html.

Run:  python3 build.py            # build from the real synced data
      python3 build.py --demo     # build from synthetic data, for layout checks only

The page is a single self-contained file: the data is embedded, so calendar.html
opens from disk, from GitHub Pages, or from an emailed copy with no server and no
network. That is the whole point of building it this way.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data" / "fixtures.json"
TEMPLATE = ROOT / "template.html"
OUT = ROOT / "calendar.html"


def demo_payload() -> dict:
    """Synthetic fixtures so the layout can be checked without touching ESPN.
    This is NOT real data and is never written to data/ or published."""
    import random
    from datetime import timedelta
    rng = random.Random(11)
    cfg = json.loads((ROOT / "config.json").read_text())
    tours, fixtures, fid = [], [], 900000
    sides = ["India", "Australia", "England", "South Africa", "Pakistan",
             "New Zealand", "Sri Lanka", "West Indies", "Bangladesh", "Afghanistan"]
    start = datetime(2026, 8, 15, tzinfo=timezone.utc)
    for i, t in enumerate(cfg["tournaments"]):
        fmt = rng.choice(["T20", "ODI", "Test"])
        n = rng.randint(3, 20)
        d0 = start + timedelta(days=rng.randint(0, 400))
        rows = []
        for m in range(n):
            d = d0 + timedelta(days=m * rng.choice([1, 2, 3]))
            a, b = rng.sample(sides, 2)
            fid += 1
            rows.append({
                "id": str(fid), "tour": t["key"],
                "utc": d.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "ts": int(d.timestamp()), "status": "NS", "status_long": "Not started",
                "round": f"{m+1}th Match", "home": a, "away": b,
                "home_score": "", "away_score": "", "venue": "Demo Ground",
                "city": "Nowhere", "country": "", "format": fmt, "note": "",
                "tbd": False, "disrupted": False, "finished": False,
                "india": "India" in (a, b),
            })
        dates = sorted(r["utc"][:10] for r in rows)
        tours.append({**t, "start": dates[0], "end": dates[-1], "matches": len(rows),
                      "india": any(r["india"] for r in rows),
                      "world": t["group"] == "ICC", "formats": [fmt]})
        fixtures += rows
    fixtures.sort(key=lambda r: r["ts"])
    return {"generated_at": "demo", "source": "SYNTHETIC DEMO DATA",
            "window_start": cfg["window_start"], "window_end": cfg["window_end"],
            "tournaments": tours, "fixtures": fixtures, "changes": [],
            "counts": {"tournaments": len(tours), "fixtures": len(fixtures),
                       "india": sum(1 for r in fixtures if r["india"]), "failures": 0},
            "failures": [], "warnings": ["SYNTHETIC DEMO DATA - not real fixtures"]}


def main() -> int:
    if "--demo" in sys.argv:
        payload = demo_payload()
        html = TEMPLATE.read_text().replace(
            "__DATA__", json.dumps(payload, separators=(",", ":")).replace("</", "<\\/"))
        out = ROOT / "preview.html"
        out.write_text(html)
        print(f"preview.html  {len(html)/1024:,.0f} KB  "
              f"{payload['counts']['fixtures']} synthetic fixtures")
        return 0

    if not DATA.exists():
        print("data/fixtures.json is missing - run sync.py first", file=sys.stderr)
        return 1
    payload = json.loads(DATA.read_text())
    if not payload.get("fixtures"):
        print("fixtures.json has no fixtures - refusing to build an empty page",
              file=sys.stderr)
        return 1

    payload["built_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    blob = json.dumps(payload, separators=(",", ":"))
    # a literal </script> inside the JSON would close the tag early
    blob = blob.replace("</", "<\\/")

    html = TEMPLATE.read_text().replace("__DATA__", blob)
    OUT.write_text(html)

    kb = len(html) / 1024
    c = payload.get("counts", {})
    print(f"calendar.html  {kb:,.0f} KB  "
          f"{c.get('fixtures', 0)} fixtures / {c.get('tournaments', 0)} tournaments")
    return 0


if __name__ == "__main__":
    sys.exit(main())
