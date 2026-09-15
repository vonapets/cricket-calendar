#!/usr/bin/env python3
"""
Measure how much each cricket competition actually trades, from Polymarket's
public Gamma API, and write the result to data/demand.json.

Run:  python3 demand.py

The calendar answers "what is on". This answers "is anyone betting on it", which
is the question a launch decision actually turns on. No key, no vendor.

Two traps, both of which quietly produce a wrong answer rather than an error:

  * **Only settled markets have volume.** A cricket market settles hours after
    the match ends, so any filter that keeps "active or future" events deletes
    every market that has ever traded. Ask that way and the whole sport looks
    like it turns over a few hundred dollars -- an earlier pass at this reported
    $250k across all of cricket, against the $90m the settled markets hold.
    `closed=true` is not optional here, it is where the data is.
  * **Side markets outnumber real ones.** Polymarket lists "India vs England",
    "India vs England - Most Sixes" and "India vs England - Toss Match Double"
    as three events. The extras trade near nothing, so counting them drags the
    median of a T20 World Cup match from $867k to $867. Only the bare "A vs B"
    title is the match market.
"""
from __future__ import annotations

import json
import re
import statistics
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
OUT = DATA / "demand.json"

GAMMA = "https://gamma-api.polymarket.com/events"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Safari/537.36")
PAGE = 100                      # the API caps a page here whatever `limit` says
PAUSE = 0.25

# A market has to have moved this much before it counts as evidence of demand.
# Polymarket lists plenty of cricket that never trades at all, and a median over
# the untraded ones measures Polymarket's listing policy, not anybody's interest.
TRADED = 1000

VS = re.compile(r"\bvs\.?\b", re.I)
SIDE = re.compile(r" - ")                       # "... - Most Sixes"
OUTRIGHT = re.compile(r"champion|winner|to win", re.I)
INDIA = re.compile(r"\bindia\b", re.I)
NOT_INDIA = re.compile(r"west indies", re.I)

# Competition identity, by Polymarket tag first and title second. Tags are
# curated and stable; titles are where the leagues without a tag show up.
TAGS = {
    "ipl": "Indian Premier League",
    "cricket-women-premier-league": "Women's Premier League",
    "csa-t20": "SA20",
    "lanka-premier-league": "Lanka Premier League",
    "cricetpl": "European T20 Premier League",
    "major-league-cricket": "Major League Cricket",
    "legends-league-cricket": "Legends League Cricket",
    "cricket-u19-world-cup": "ICC U19 World Cup",
    "thunderbolt-t10-league": "Thunderbolt T10",
    "national-t20-cup": "Pakistan National T20",
    "sheffield-shield": "Sheffield Shield",
}
TITLES = [(re.compile(p, re.I), n) for p, n in [
    (r"^T20 World Cup:|T20 Men.s World Cup", "ICC Men's T20 World Cup"),
    (r"Big Bash|\bBBL\b", "Big Bash League"),
    (r"The Hundred", "The Hundred"),
    (r"Caribbean Premier|\bCPL\b", "Caribbean Premier League"),
    (r"\bPSL\b|Pakistan Super", "Pakistan Super League"),
    (r"\bMLC\b|Major League Cricket", "Major League Cricket"),
    (r"\bILT20\b", "ILT20"),
    (r"\bSA20\b", "SA20"),
    (r"Asia Cup", "Asia Cup"),
    (r"Champions Trophy", "ICC Champions Trophy"),
    (r"ODI World Cup|Cricket World Cup", "ICC ODI World Cup"),
]]
INTERNATIONAL = {"international-cricket", "international-t20", "odi"}

# Demand for a bilateral tour is a fact about the two teams, not about the
# format, so the per-team and per-pair medians below are what a tour with no
# listing history of its own gets estimated from. "Other international" as a
# single number is useless: it puts Australia v Zimbabwe and an Ashes Test in
# the same bucket and quotes $57k for both.
NATIONS = ["india", "australia", "england", "south africa", "pakistan", "new zealand",
           "sri lanka", "west indies", "bangladesh", "afghanistan", "ireland", "zimbabwe",
           "netherlands", "scotland", "namibia", "nepal", "oman", "usa", "canada", "uae",
           "united arab emirates", "zimbabwe", "papua new guinea", "uganda", "italy"]
# longest first, so "west indies" is not read as "indies" and "south africa"
# beats a bare "africa"
NATIONS = sorted(set(NATIONS), key=len, reverse=True)
QUALIFIER = re.compile(r"\b(women|under-?19s?|u-?19s?|\ba\b|emerging|legends)\b", re.I)


def sides(title: str) -> tuple[str, str] | None:
    """The two national teams in a Polymarket title, or None if it is not one.

    Titles arrive as "India ODI Series: India vs South Africa (Game 2)", so the
    part before a colon is the series label and has to go before splitting, or
    every series name is read as a team.
    """
    t = title.split(":")[-1]
    t = re.sub(r"\([^)]*\)", " ", t)
    parts = VS.split(t)
    if len(parts) != 2:
        return None
    out = []
    for p in parts:
        p = p.strip().lower()
        if QUALIFIER.search(p):
            return None                         # Women / U19 / A are separate draws
        hit = next((n for n in NATIONS if n in p), None)
        if not hit:
            return None
        out.append("united arab emirates" if hit == "uae" else hit)
    return (out[0], out[1]) if out[0] != out[1] else None

# What a calendar row has to look like to inherit a competition's numbers. The
# calendar names a tour after its two countries and Polymarket names it after
# the format, so the two never match on text; these are the bridge.
DETECT = {
    "Indian Premier League": ["indian premier league"],
    "Women's Premier League": ["women's premier league", "womens premier league"],
    "Big Bash League": ["big bash"],
    "SA20": ["sa20"],
    "ILT20": ["ilt20", "international league t20"],
    "Caribbean Premier League": ["caribbean premier league"],
    "European T20 Premier League": ["european t20 premier league"],
    "Lanka Premier League": ["lanka premier league"],
    "Major League Cricket": ["major league cricket"],
    "The Hundred": ["the hundred"],
    "Asia Cup": ["asia cup"],
    "ICC Men's T20 World Cup": ["t20 world cup"],
    "ICC ODI World Cup": ["odi world cup", "cricket world cup"],
}
NOT_DETECT = {
    "ILT20": ["africa continent"],
    "Big Bash League": ["women"],
    "Indian Premier League": ["women"],
    "SA20": ["women"],
}


def fetch(url: str, retries: int = 3) -> list:
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=45) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            # Walking off the end of the list is a 422 here, not an empty page,
            # so a paginator that only stops on `[]` never stops.
            if exc.code in (404, 422):
                return []
            last = exc
            time.sleep(1.5 * (attempt + 1))
        except Exception as exc:                # noqa: BLE001 - retry then report
            last = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"giving up on {url}: {last}")


def pull() -> list[dict]:
    """Every cricket event Polymarket has, settled ones included."""
    events, seen = [], set()
    for closed in ("true", "false"):
        offset = 0
        while True:
            url = (f"{GAMMA}?tag_slug=cricket&closed={closed}"
                   f"&limit={PAGE}&offset={offset}")
            batch = fetch(url)
            if not batch:
                break
            for e in batch:
                if e.get("id") not in seen:
                    seen.add(e["id"])
                    events.append(e)
            offset += len(batch)
            if len(batch) < PAGE:
                break
            time.sleep(PAUSE)
        print(f"  {closed=}: {offset} events scanned")
    return events


def competition(ev: dict) -> str:
    tags = {t.get("slug") for t in (ev.get("tags") or [])}
    title = ev.get("title") or ""
    for rx, name in TITLES:
        if rx.search(title):
            return name
    for slug, name in TAGS.items():
        if slug in tags:
            return name
    if tags & INTERNATIONAL:
        if INDIA.search(title) and not NOT_INDIA.search(title):
            return "India bilateral"
        return "Other international"
    return "Domestic / minor"


def summarise(vols: list[float], outright: float, n_india: int, n: int) -> dict:
    traded = sorted(v for v in vols if v >= TRADED)
    return {
        "matches": n,
        "traded": len(traded),
        "median": round(statistics.median(traded)) if traded else 0,
        "p75": round(traded[int(len(traded) * 0.75)]) if traded else 0,
        "best": round(max(vols)) if vols else 0,
        "total": round(sum(vols)),
        "outright": round(outright),
        "india_pct": round(100 * n_india / n) if n else 0,
    }


def main() -> int:
    try:
        events = pull()
    except Exception as exc:                    # noqa: BLE001
        print(f"Polymarket pull failed ({exc}) - keeping the previous snapshot",
              file=sys.stderr)
        return 1
    if len(events) < 200:
        print(f"only {len(events)} events came back; that is too few to trust "
              f"- keeping the previous snapshot", file=sys.stderr)
        return 1

    match_vols: dict[str, list[float]] = {}
    outrights: dict[str, float] = {}
    india_n: dict[str, int] = {}
    team_vols: dict[str, list[float]] = {}
    pair_vols: dict[str, list[float]] = {}
    dates = []

    for ev in events:
        title = ev.get("title") or ""
        vol = float(ev.get("volume") or 0)
        comp = competition(ev)
        if ev.get("startDate"):
            dates.append(ev["startDate"][:10])
        if SIDE.search(title):
            continue                            # a prop, not the match market
        if OUTRIGHT.search(title):
            outrights[comp] = outrights.get(comp, 0.0) + vol
            continue
        if not VS.search(title):
            continue
        match_vols.setdefault(comp, []).append(vol)
        if INDIA.search(title) and not NOT_INDIA.search(title):
            india_n[comp] = india_n.get(comp, 0) + 1
        # Team medians are built from bilateral cricket only. A World Cup match
        # trades about a million whoever is playing, so counting it here makes
        # Namibia look like a $884k draw on the strength of six World Cup games
        # and turns the estimate for any tour they play into fiction.
        if comp in ("India bilateral", "Other international"):
            pair = sides(title)
            if pair:
                for team in pair:
                    team_vols.setdefault(team, []).append(vol)
                pair_vols.setdefault("|".join(sorted(pair)), []).append(vol)

    comps = {}
    for comp, vols in match_vols.items():
        c = summarise(vols, outrights.get(comp, 0.0), india_n.get(comp, 0), len(vols))
        c["name"] = comp
        c["detect"] = DETECT.get(comp, [])
        c["not"] = NOT_DETECT.get(comp, [])
        comps[comp] = c
    # an outright-only competition still belongs in the table: the IPL's
    # Champion market is worth more than its entire match card
    for comp, vol in outrights.items():
        if comp not in comps:
            comps[comp] = {"name": comp, "matches": 0, "traded": 0, "median": 0,
                           "p75": 0, "best": round(vol), "total": 0,
                           "outright": round(vol), "india_pct": 0,
                           "detect": DETECT.get(comp, []), "not": NOT_DETECT.get(comp, [])}

    def band(vols: list[float]) -> dict:
        traded = sorted(v for v in vols if v >= TRADED)
        return {"n": len(vols), "traded": len(traded),
                "median": round(statistics.median(traded)) if traded else 0,
                "best": round(max(vols)) if vols else 0}

    teams = {k: band(v) for k, v in team_vols.items() if len(v) >= 2}
    pairs = {k: band(v) for k, v in pair_vols.items() if len(v) >= 2}

    dates.sort()
    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "Polymarket Gamma API (gamma-api.polymarket.com), settled markets included",
        "traded_floor": TRADED,
        "observed_from": dates[0] if dates else None,
        "observed_to": dates[-1] if dates else None,
        "totals": {"events": len(events),
                   "volume": round(sum(float(e.get("volume") or 0) for e in events)),
                   "competitions": len(comps)},
        "competitions": comps,
        "teams": dict(sorted(teams.items(), key=lambda kv: -kv[1]["median"])),
        "pairs": dict(sorted(pairs.items(), key=lambda kv: -kv[1]["median"])),
    }
    OUT.write_text(json.dumps(payload, indent=1))

    print(f"\n{len(events)} events, ${payload['totals']['volume']:,} traded, "
          f"{len(comps)} competitions, {dates[0]} to {dates[-1]}")
    for c in sorted(comps.values(), key=lambda x: -x["median"])[:10]:
        print(f"  {c['name'][:30]:<31}{c['traded']:>4} traded  "
              f"median ${c['median']:>9,}  total ${c['total']:>12,}")
    print(f"\nper-team medians ({len(teams)} teams, {len(pairs)} pairs):")
    for t, b in list(payload["teams"].items())[:10]:
        print(f"  {t[:22]:<23}{b['traded']:>4} traded of {b['n']:<4} median ${b['median']:>9,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
