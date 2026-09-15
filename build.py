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
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import demand as dmd               # NATIONS / QUALIFIER, so team names are read one way

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data" / "fixtures.json"
DEMAND = ROOT / "data" / "demand.json"
TEMPLATE = ROOT / "template.html"
OUT = ROOT / "calendar.html"

# Months of calendar the playbook plans over. Past this the feed has not
# published enough for a launch sequence to mean anything.
HORIZON_MONTHS = 12

# Median volume of one match market, in dollars, and what to do about it.
# The bands are wide on purpose: the difference between $27k and $33k a match is
# noise, the difference between $250k and $250 is the whole decision.
VERDICTS = [
    (200_000, "launch",  "Launch"),
    (50_000,  "strong",  "Launch"),
    (15_000,  "worth",   "Worth listing"),
    (1_000,   "thin",    "Thin"),
    (0,       "skip",    "Do not list"),
]


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


def verdict(median: int) -> tuple[str, str]:
    for floor, key, label in VERDICTS:
        if median >= floor:
            return key, label
    return "skip", "Do not list"


def tour_sides(name: str) -> tuple[str, str] | None:
    """The two countries in a calendar row, for rows that are a bilateral tour."""
    n = re.sub(r"\[[^\]]*\]", " ", name.lower())
    if dmd.QUALIFIER.search(n):
        return None                             # A / Under-19 / Women are separate draws
    parts = re.split(r"\s+tour of\s+|\s+vs?\.?\s+", n)
    if len(parts) != 2:
        return None
    out = []
    for part in parts:
        hit = next((x for x in dmd.NATIONS if x in part), None)
        if not hit:
            return None
        out.append("united arab emirates" if hit == "uae" else hit)
    return (out[0], out[1]) if out[0] != out[1] else None


def match_demand(tour: dict, dem: dict) -> dict:
    """What one calendar row can be expected to trade, and how sure that is.

    Four answers, in descending order of how much they are worth:

      measured  the competition has its own settled history (the Big Bash has 47)
      pair      this exact fixture has been listed before (India v New Zealand, 8)
      range     neither, but both countries have bilateral history of their own,
                so the honest answer is a range and not a number
      none      no comparable at all

    The range matters more than it looks. A single "India bilateral" number says
    a Sri Lanka tour of India is worth $247k a match, because that is what India
    is worth on average. India v Afghanistan, listed right now, is trading
    $37k. The premium is real and it is opponent-dependent, and a playbook that
    hides that behind one number is telling you something false with confidence.
    """
    comps, teams, pairs = dem["competitions"], dem["teams"], dem["pairs"]
    name = tour["name"].lower()

    for c in comps.values():
        if not c.get("detect"):
            continue
        if any(d in name for d in c["detect"]) and \
           not any(x in name for x in c.get("not", [])):
            if c["traded"] >= 5:
                return {"low": c["median"], "high": c["median"], "point": c["median"],
                        "outright": c.get("outright", 0), "confidence": "measured",
                        "evidence": c["name"], "n": c["traded"]}
            # listed, but too thin to call either way
            return {"low": 0, "high": c["median"], "point": 0,
                    "outright": c.get("outright", 0), "confidence": "thin-sample",
                    "evidence": c["name"], "n": c["traded"]}

    pair = tour_sides(tour["name"])
    if pair:
        key = "|".join(sorted(pair))
        pb = pairs.get(key)
        if pb and pb["traded"] >= 3:
            return {"low": pb["median"], "high": pb["median"], "point": pb["median"],
                    "outright": 0, "confidence": "pair",
                    "evidence": f"{pair[0].title()} v {pair[1].title()}", "n": pb["traded"]}
        a, b = teams.get(pair[0]), teams.get(pair[1])
        if a and b and a["traded"] >= 2 and b["traded"] >= 2:
            lo, hi = sorted((a["median"], b["median"]))
            thin = min(a["traded"], b["traded"]) < 4
            return {"low": lo, "high": hi, "point": lo, "outright": 0,
                    "confidence": "sparse" if thin else "range",
                    "evidence": f"{pair[0].title()} {a['median']:,} / "
                                f"{pair[1].title()} {b['median']:,}",
                    "n": a["traded"] + b["traded"]}

    return {"low": 0, "high": 0, "point": 0, "outright": 0,
            "confidence": "none", "evidence": "no comparable", "n": 0}


def playbook(fixtures: dict, demand: dict) -> dict:
    """The calendar's next twelve months, ranked and judged on measured demand."""
    comps = demand.get("competitions", {})
    today = datetime.now(timezone.utc).date()
    end = today.replace(year=today.year + (HORIZON_MONTHS // 12))
    rows = []
    for t in fixtures.get("tournaments", []):
        if t.get("tier") != "major" or not t.get("start"):
            continue
        if t["end"] < today.isoformat() or t["start"] > end.isoformat():
            continue
        d = match_demand(t, demand)
        n = t.get("matches", 0)
        # judged on the floor, not the ceiling: a launch is committed to before
        # the volume shows up, so the number that matters is the bad case
        key, label = verdict(d["point"])
        # The IPL trades $33k a match and $3.8m on one Champion market. Ranked
        # on its match card it reads as a mid-table league, which is the wrong
        # conclusion about the biggest competition in the sport - the product
        # to list is the outright, not 74 individual games.
        if d["outright"] >= 1_000_000:
            key, label = "prize", "List the outright"
        elif d["confidence"] == "none":
            key, label = "unknown", "No comparable"
        elif d["confidence"] == "sparse" and key in ("launch", "strong"):
            label = "Launch — thin evidence"
        rows.append({
            "key": t["key"], "name": t["name"], "short": t.get("short", ""),
            "start": t["start"], "end": t["end"], "matches": n,
            "planned": bool(t.get("planned")),
            "india": bool(t.get("india") or t.get("in_india")),
            "world": bool(t.get("world")),
            "formats": t.get("formats") or [],
            "confidence": d["confidence"], "evidence": d["evidence"], "traded": d["n"],
            "low": d["low"], "high": d["high"], "median": d["point"],
            "outright": d["outright"],
            "est_low": d["low"] * n + d["outright"],
            "est_high": d["high"] * n + d["outright"],
            "verdict": key, "verdict_label": label,
        })
    rows.sort(key=lambda r: r["start"])
    live = [r for r in rows if r["verdict"] in ("launch", "strong", "worth", "prize")]
    return {
        "horizon_end": end.isoformat(),
        "rows": rows,
        "totals": {
            "listable": len(live),
            "matches": sum(r["matches"] for r in live),
            "est_low": sum(r["est_low"] for r in live),
            "est_high": sum(r["est_high"] for r in live),
            "skip": sum(1 for r in rows if r["verdict"] == "skip"),
            "unknown": sum(1 for r in rows if r["verdict"] == "unknown"),
        },
    }


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

    if DEMAND.exists():
        payload["demand"] = json.loads(DEMAND.read_text())
        payload["playbook"] = playbook(payload, payload["demand"])
    else:
        print("data/demand.json is missing - run demand.py for the playbook",
              file=sys.stderr)
        payload["demand"], payload["playbook"] = None, None

    blob = json.dumps(payload, separators=(",", ":"))
    # a literal </script> inside the JSON would close the tag early
    blob = blob.replace("</", "<\\/")

    html = TEMPLATE.read_text().replace("__DATA__", blob)
    OUT.write_text(html)

    kb = len(html) / 1024
    c = payload.get("counts", {})
    pb = payload.get("playbook") or {}
    print(f"calendar.html  {kb:,.0f} KB  "
          f"{c.get('fixtures', 0)} fixtures / {c.get('tournaments', 0)} tournaments"
          + (f" / playbook: {pb['totals']['listable']} to list, "
             f"{pb['totals']['skip']} to skip" if pb else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
