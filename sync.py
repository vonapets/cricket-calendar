#!/usr/bin/env python3
"""
Pull cricket fixtures from ESPN's public feed, normalise them, diff against the
previous snapshot to catch reschedules, and write the result to data/.

Run:  python3 sync.py

No API key. This is the same undocumented JSON endpoint espn.com's own cricket
pages use, so sync.py is defensive: it validates every response, refuses to
overwrite good data with a bad fetch, and keeps the previous snapshot on failure.

Two things differ from the football feed this project is modelled on:

  * Cricket has no date-range query. `?dates=<year>` returns a whole season and
    a day-stamped `?dates=YYYYMMDD` silently returns one match, so every pull is
    per tournament per season.
  * Tournaments are not a fixed list. Bilateral tours get a fresh ESPN league id
    each time they are played, so config.json carries the recurring competitions
    and discover() picks up whatever else is live from ESPN's own header feed.
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
RAW = DATA / "raw"
DATA.mkdir(exist_ok=True)

FIXTURES_FILE = DATA / "fixtures.json"
CHANGES_FILE = DATA / "changes.json"
REGISTRY_FILE = DATA / "registry.json"
CONFIG_FILE = ROOT / "config.json"

BASE = "https://site.api.espn.com/apis/site/v2/sports/cricket/{lid}/scoreboard"
HEADER = "https://site.web.api.espn.com/apis/v2/scoreboard/header?sport=cricket"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Safari/537.36")

STATUS_MAP = {
    "STATUS_SCHEDULED":   ("NS",   "Not started"),
    "STATUS_IN_PROGRESS": ("LIVE", "In progress"),
    "STATUS_FINAL":       ("FT",   "Result"),
    "STATUS_ABANDONED":   ("ABD",  "Abandoned"),
    "STATUS_CANCELED":    ("CANC", "Cancelled"),
    "STATUS_CANCELLED":   ("CANC", "Cancelled"),
    "STATUS_POSTPONED":   ("PST",  "Postponed"),
    "STATUS_SUSPENDED":   ("SUSP", "Suspended"),
    "STATUS_DELAYED":     ("SUSP", "Delayed"),
    "STATUS_RAIN":        ("RAIN", "Rain delay"),
    "STATUS_NO_RESULT":   ("NR",   "No result"),
    "STATUS_TIE":         ("TIE",  "Tied"),
    "STATUS_DRAWN":       ("DRW",  "Drawn"),
}
DISRUPTED = {"PST", "CANC", "SUSP", "ABD", "NR"}

# A season pull that comes back at exactly this size is suspect, not complete.
TRUNCATION_TRIPWIRE = 1000

# seconds between requests - see the sleep in main()
PAUSE = 0.4


def fetch(url: str, retries: int = 4) -> dict:
    """GET one JSON document. Raises on give-up."""
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=45) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise
            last = exc
            time.sleep(2 * (attempt + 1))
        except Exception as exc:                    # noqa: BLE001 - retry then report
            last = exc
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"giving up on {url}: {last}")


def discover() -> list[dict]:
    """Whatever cricket ESPN currently has live.

    This is how bilateral tours reach the calendar at all. `Bangladesh A tour of
    South Africa 2026` is league id 24694 and will never be that id again, so it
    cannot be configured ahead of time -- it has to be noticed while it is on.
    """
    try:
        head = fetch(HEADER)
    except Exception as exc:                        # noqa: BLE001 - non-fatal
        print(f"  ! discovery failed ({exc}); configured tournaments only")
        return []
    out = []
    for sport in head.get("sports") or []:
        for lg in sport.get("leagues") or []:
            if lg.get("id") and lg.get("name"):
                out.append({"id": str(lg["id"]), "name": lg["name"],
                            "short": lg.get("abbreviation") or lg["name"][:18]})
    return out


def load_registry() -> dict:
    """Tournaments seen on previous runs, so a tour stays on the calendar after
    it drops off ESPN's live header."""
    if REGISTRY_FILE.exists():
        try:
            return json.loads(REGISTRY_FILE.read_text())
        except Exception:                           # noqa: BLE001
            pass
    return {}


# --- classification -------------------------------------------------------
# The two properties that actually drive interest, carried as flags so the page
# can colour by them: matches involving India, and global (ICC) tournaments.

INDIA = re.compile(r"\bindia\b", re.I)
NOT_INDIA = re.compile(r"india\s+(a|under|u19|u-19|women)|west indies", re.I)
GLOBAL = re.compile(r"\b(world cup|champions trophy|world test championship|"
                    r"asia cup|olympic|commonwealth|asian games)\b", re.I)


def is_india(*names: str) -> bool:
    for n in names:
        if not n:
            continue
        # "West Indies" contains "Indies", not "India"; the word-boundary regex
        # already excludes it, but India A / U19 / Women are separate draws.
        if INDIA.search(n) and not NOT_INDIA.search(n):
            return True
    return False


# A tour between two Test nations is worth charting. The same countries' A,
# Under-19 and second-XI sides are not, and nor is provincial cricket: the feed
# hands back 610 matches of it, which is enough to bury the IPL. Both are kept,
# but the page shows only the first by default - the line the PDF drew when it
# charted "every club league and major tour worth knowing about".
NON_SENIOR = re.compile(r"\b(under-?19s?|u-?19s?|under-?23s?|emerging|development|"
                        r"second eleven|2nd xi|academy)\b", re.I)
# case-sensitive on purpose: "Australia A tour of India" is an A team, and a
# lower-case "a" is just the English article waiting to demote the wrong row.
A_TEAM = re.compile(r"\bA\b")
WOMEN = re.compile(r"\bwomen'?s?\b|\(women\)", re.I)


def tier_of(name: str, rows: list[dict], cfg: dict) -> str:
    low = name.lower()
    # first, because ESPN names an associate event after whoever sanctions it:
    # "ILT20 Africa Continent cup" is Rwanda v Botswana, not the ILT20.
    if any(c in low for c in cfg.get("minor_overrides", [])):
        return "minor"
    if WOMEN.search(name):
        # the PDF charted exactly one women's competition out of 27, the WPL
        return "major" if any(c in low for c in cfg.get("major_women_competitions", [])) \
               else "minor"
    if any(c in low for c in cfg.get("major_competitions", [])):
        return "major"                              # a named league or ICC event
    if NON_SENIOR.search(name) or A_TEAM.search(name):
        return "minor"
    members = set(cfg.get("full_members", []))
    for r in rows:
        if r["home"].lower() in members and r["away"].lower() in members:
            return "major"                          # a senior bilateral tour
    # one Test nation is not enough: "South Africa tour of Namibia" stays minor.
    return "minor"


def is_india_comp(name: str, rows: list[dict], cfg: dict) -> bool:
    """Whether the competition itself is an India draw.

    is_india() asks whether the India team is on the field, which is the right
    question for a fixture and the wrong one for the IPL: "Mumbai Indians" is
    not "India", so every IPL match would read as nothing to do with India and
    the one competition the memo calls the prize would sit outside the India
    filter.
    """
    low = name.lower()
    if any(c in low for c in cfg.get("india_competitions", [])):
        return True
    if " tour of india" in low:
        return True
    return bool(rows) and sum(
        1 for r in rows if (r.get("country") or "").lower() == "india") > len(rows) / 2


def normalise(lid: str, tkey: str, doc: dict, cfg_meta: dict) -> tuple[list[dict], dict]:
    """One ESPN season document -> our fixture rows plus tournament metadata."""
    league = (doc.get("leagues") or [{}])[0]
    events = doc.get("events") or []
    rows: list[dict] = []

    for ev in events:
        comp = (ev.get("competitions") or [{}])[0]
        status = ((comp.get("status") or {}).get("type") or {}) or {}
        code, long = STATUS_MAP.get(status.get("name") or "", (None, None))
        if code is None:
            code = (status.get("shortDetail") or status.get("name") or "?")[:6]
            long = status.get("detail") or status.get("description") or code

        sides = comp.get("competitors") or []
        def side(i: int, field: str, default=None):
            try:
                return (sides[i].get("team") or {}).get(field, default)
            except IndexError:
                return default

        home = side(0, "displayName") or "TBC"
        away = side(1, "displayName") or "TBC"
        venue = (comp.get("venue") or {})
        addr = venue.get("address") or {}
        klass = comp.get("class") or {}

        utc = ev.get("date")
        if not utc:
            continue
        # ESPN stamps cricket as YYYY-MM-DDTHH:MMZ, minus the seconds.
        iso = utc if len(utc) > 17 else utc.replace("Z", ":00Z")
        try:
            ts = int(datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ")
                     .replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            continue

        rows.append({
            "id": str(ev.get("id")),
            "tour": tkey,
            "utc": iso,
            "ts": ts,
            "status": code,
            "status_long": long,
            "round": comp.get("description") or "",
            "home": home,
            "away": away,
            "home_score": (sides[0].get("score") if len(sides) > 0 else None) or "",
            "away_score": (sides[1].get("score") if len(sides) > 1 else None) or "",
            "venue": venue.get("fullName") or "",
            "city": addr.get("city") or "",
            "country": addr.get("country") or "",
            "format": klass.get("eventType") or klass.get("generalClassCard") or "",
            "note": (status.get("detail") or "")[:120],
            "tbd": not comp.get("timeValid", True),
            "disrupted": code in DISRUPTED,
            "finished": bool(status.get("completed")),
            "india": is_india(home, away),
        })

    dates = sorted(r["utc"][:10] for r in rows)
    name = cfg_meta.get("name") or league.get("name") or f"League {lid}"
    meta = {
        "key": tkey,
        "id": lid,
        "name": name,
        "short": cfg_meta.get("short") or league.get("abbreviation") or name[:18],
        "group": cfg_meta.get("group") or "Other",
        "color": cfg_meta.get("color") or "#7a8899",
        "start": dates[0] if dates else None,
        "end": dates[-1] if dates else None,
        "matches": len(rows),
        "india": any(r["india"] for r in rows),
        "world": bool(GLOBAL.search(name)),
        "formats": sorted({r["format"] for r in rows if r["format"]}),
    }
    return rows, meta


def project(cfg: dict, tours: list[dict]) -> list[dict]:
    """The planned tournaments that the feed has not superseded."""
    horizon = cfg.get("projection_horizon", "")
    real_names = [t["name"].lower() for t in tours]
    out = []
    for pl in cfg.get("planned", []):
        if horizon and pl["start"] > horizon:
            continue
        if any(any(d in nm for d in pl["detect"])
               and not any(x in nm for x in pl.get("not", []))
               for nm in real_names):
            continue                                # the feed has the real thing
        out.append({
            "key": pl["key"], "id": None, "name": pl["name"], "short": pl["short"],
            "group": pl["group"], "color": pl["color"],
            "start": pl["start"], "end": pl["end"],
            "matches": pl["matches"] or 0, "expected": pl["matches"],
            "india": False,
            "in_india": any(c in pl["name"].lower()
                            for c in cfg.get("india_competitions", [])),
            "world": bool(GLOBAL.search(pl["name"])),
            "formats": pl.get("formats", []), "tier": "major", "planned": True,
        })
    return out


def main() -> int:
    cfg = json.loads(CONFIG_FILE.read_text())
    win_start, win_end = cfg["window_start"], cfg["window_end"]
    seasons = cfg["seasons"]

    registry = load_registry()
    # configured recurring competitions, then anything ESPN says is live now
    targets: dict[str, dict] = {}
    for t in cfg["tournaments"]:
        targets[str(t["id"])] = dict(t)
    for t in registry.values():
        targets.setdefault(str(t["id"]), dict(t))
    found = discover()
    print(f"  discovery: {len(found)} live tournaments from ESPN header")
    for t in found:
        if t["id"] not in targets:
            targets[t["id"]] = {"id": t["id"], "name": t["name"], "short": t["short"],
                                "group": "Discovered", "color": "#7a8899"}

    fixtures: list[dict] = []
    tours: list[dict] = []
    # ESPN nests its containers: "West Indies tour of India 2026/27" and
    # "West Indies in India T20I Series 2026/27" are separate league ids over
    # the same matches. Both are worth requesting -- whichever is populated
    # wins -- but a match must only be counted once, so ids are claimed globally.
    claimed: set[str] = set()
    failures: list[str] = []
    warnings: list[str] = []
    unpublished: list[dict] = []
    RAW.mkdir(exist_ok=True)

    # Order decides who claims a shared match, so it is deliberate rather than
    # alphabetical: configured competitions first, then whole tours ahead of the
    # per-format series nested inside them. One "West Indies tour of India" row
    # reads better on a wallchart than three rows for its Test, ODI and T20I legs.
    def priority(kv):
        lid, meta = kv
        configured = 0 if meta.get("key") else 1
        name = meta.get("name", "")
        whole_tour = 0 if " tour of " in name.lower() else 1
        return (configured, whole_tour, name)

    for lid, meta in sorted(targets.items(), key=priority):
        key = meta.get("key") or f"t{lid}"
        got_any = False
        rows_all: list[dict] = []
        tour_meta = None
        for season in seasons:
            url = f"{BASE.format(lid=lid)}?dates={season}"
            try:
                doc = fetch(url)
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    continue                        # league had no such season
                failures.append(f"{meta.get('name', lid)} {season}: HTTP {exc.code}")
                continue
            except Exception as exc:                # noqa: BLE001
                failures.append(f"{meta.get('name', lid)} {season}: {exc}")
                continue
            # ESPN throttles a fast sweep of this endpoint hard, and a throttled
            # pull looks like an empty season rather than an error. Pace it.
            time.sleep(PAUSE)
            (RAW / f"{lid}_{season}.json").write_text(json.dumps(doc))
            rows, tm = normalise(lid, key, doc, meta)
            if len(rows) >= TRUNCATION_TRIPWIRE:
                warnings.append(f"{tm['name']} {season} hit the truncation tripwire "
                                f"({len(rows)} events) - may be incomplete")
            rows_all += rows
            tour_meta = tm if tour_meta is None else tour_meta
            got_any = True

        if not got_any or not rows_all:
            continue

        # keep only what falls inside the calendar window
        outside = [r for r in rows_all if not win_start <= r["utc"][:10] <= win_end]
        rows_all = [r for r in rows_all if win_start <= r["utc"][:10] <= win_end]
        if not rows_all:
            # Every match this league has falls outside the window. For a
            # recurring competition that does not mean "no such tournament", it
            # means the last season is over and the next one is not published
            # yet -- which is exactly where the IPL sits for most of the year.
            # Both used to take this same silent `continue`, so the biggest
            # competition in the sport left the chart without a word in the log.
            if outside:
                seen = sorted(r["utc"][:10] for r in outside)
                # recorded, not warned about: for a recurring competition this
                # is the normal state between seasons, and 21 of them would
                # drown the one warning that means something is actually wrong.
                unpublished.append({"id": lid, "name": meta.get("name", lid),
                                    "matches": len(outside),
                                    "last_seen": f"{seen[0]} to {seen[-1]}"})
            continue

        deduped = []
        for r in rows_all:      # a match can appear in two seasons, or two containers
            if r["id"] in claimed:
                continue
            claimed.add(r["id"])
            deduped.append(r)

        if not deduped:                             # fully covered by another container
            continue

        dates = sorted(r["utc"][:10] for r in deduped)
        tour_meta.update({"start": dates[0], "end": dates[-1],
                          "matches": len(deduped),
                          "india": any(r["india"] for r in deduped),
                          "in_india": is_india_comp(tour_meta["name"], deduped, cfg),
                          "tier": tier_of(tour_meta["name"], deduped, cfg),
                          "planned": False})
        fixtures += deduped
        tours.append(tour_meta)
        registry[lid] = {"id": lid, "key": key, "name": tour_meta["name"],
                         "short": tour_meta["short"], "group": tour_meta["group"],
                         "color": tour_meta["color"]}
        print(f"  {tour_meta['name'][:48]:<48} {len(deduped):>4} matches  "
              f"{tour_meta['start']} -> {tour_meta['end']}")

    # --- projected tournaments -------------------------------------------
    # A feed can only show what somebody has already published, and in September
    # a 2027 season mostly has not been: ESPN answers `?dates=2027` for the IPL
    # with an empty list, not with next April. The wallchart in
    # The-Cricket-Window.pdf carries those tournaments anyway, so config.planned
    # carries them here too -- a band and an expected match count, never an
    # invented fixture -- and each one is dropped the moment the feed has the
    # real thing. Past projection_horizon nothing is projected at all: that far
    # out the PDF's dates are a guess about an unpublished season, and a guess
    # that old is worth less to a trading desk than an honest gap.
    horizon = cfg.get("projection_horizon", "")
    projected = project(cfg, tours)
    tours += projected

    if not fixtures:
        print("no fixtures pulled at all - keeping the previous snapshot", file=sys.stderr)
        return 1

    # A pull that collapses to a fraction of the last good one is a bad fetch,
    # not a quiet cricket season. Refuse it rather than publish a gutted page.
    if FIXTURES_FILE.exists():
        try:
            prev_doc = json.loads(FIXTURES_FILE.read_text())
            prev_n = len(prev_doc.get("fixtures") or [])
            if prev_n and len(fixtures) < prev_n * 0.6:
                print(f"refusing to overwrite: {len(fixtures)} fixtures vs "
                      f"{prev_n} previously ({len(failures)} failures)", file=sys.stderr)
                return 1
        except Exception:                           # noqa: BLE001
            prev_doc = {}
    else:
        prev_doc = {}

    changes = diff(prev_doc, fixtures)
    fixtures.sort(key=lambda r: (r["ts"], r["tour"]))
    tours.sort(key=lambda t: (t["start"] or "", t["name"]))

    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "ESPN public cricket feed (site.api.espn.com)",
        "window_start": win_start,
        "window_end": win_end,
        "tournaments": tours,
        "fixtures": fixtures,
        "changes": changes,
        "counts": {"tournaments": len(tours), "fixtures": len(fixtures),
                   "india": sum(1 for r in fixtures if r["india"]),
                   "major": sum(1 for t in tours if t.get("tier") == "major"),
                   "projected": len(projected),
                   "projected_matches": sum(t["matches"] for t in projected),
                   "failures": len(failures)},
        "projection_horizon": horizon,
        "unpublished": unpublished,
        "failures": failures,
        "warnings": warnings,
    }
    FIXTURES_FILE.write_text(json.dumps(payload, indent=1))
    CHANGES_FILE.write_text(json.dumps(changes, indent=1))
    REGISTRY_FILE.write_text(json.dumps(registry, indent=1))

    print(f"\n{len(fixtures)} fixtures across {len(tours)} tournaments "
          f"({sum(1 for t in tours if t.get('tier') == 'major')} major, "
          f"{len(projected)} projected), {len(changes)} changes, {len(failures)} failures")
    for u in unpublished:
        print(f"  - {u['name'][:44]:<44} no published season in window "
              f"(last: {u['last_seen']})")
    for w in warnings:
        print(f"  ! {w}")
    for f in failures[:10]:
        print(f"  x {f}")
    return 0


def diff(prev_doc: dict, now: list[dict]) -> list[dict]:
    """Reschedules, cancellations and new listings since the last snapshot."""
    prev = {r["id"]: r for r in (prev_doc.get("fixtures") or [])}
    out = []
    for r in now:
        old = prev.get(r["id"])
        if not old:
            if prev:                                # not the very first run
                out.append({"kind": "added", "id": r["id"], "tour": r["tour"],
                            "what": f"{r['home']} v {r['away']}", "to": r["utc"]})
            continue
        if old.get("utc") != r["utc"]:
            out.append({"kind": "moved", "id": r["id"], "tour": r["tour"],
                        "what": f"{r['home']} v {r['away']}",
                        "from": old.get("utc"), "to": r["utc"]})
        if old.get("status") != r["status"] and r["disrupted"]:
            out.append({"kind": "disrupted", "id": r["id"], "tour": r["tour"],
                        "what": f"{r['home']} v {r['away']}",
                        "from": old.get("status"), "to": r["status_long"]})
    now_ids = {r["id"] for r in now}
    for pid, old in prev.items():
        if pid not in now_ids and not old.get("finished"):
            out.append({"kind": "dropped", "id": pid, "tour": old.get("tour"),
                        "what": f"{old.get('home')} v {old.get('away')}",
                        "from": old.get("utc")})
    return out


if __name__ == "__main__":
    sys.exit(main())
