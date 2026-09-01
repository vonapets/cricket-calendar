# Cricket Wallchart

One page showing every cricket tournament and tour worth knowing about, when it
runs, how many matches it has, and which of them involve India or an ICC event.
Sibling project to [football-calendar](https://github.com/vonapets/football-calendar);
same shape, different sport and a data source that behaves quite differently.

**Live page:** https://vonapets.github.io/cricket-calendar/

## Why this exists

Cricket is not a season, it is a queue of tournaments that overlap. A franchise
league runs for six weeks, a bilateral tour drops three T20Is into the middle of
it, and an ICC event clears the decks for a month. There is no single fixture
list, so the question "what is actually on in September?" is genuinely hard to
answer from the sport's own websites.

It was built to answer a specific version of that question — which cricket
competition is worth listing on a prediction market, and when — so the page
carries that bias openly: **India fixtures and ICC world tournaments are marked
amber**, because on Polymarket those two draw multiples of the volume of
everything else. A typical India match trades around $300k against roughly $50k
for a non-India international and about $44k for a Caribbean Premier League game.

## How it works

```
sync.py     ESPN public feed -> data/fixtures.json  (+ changes.json, registry.json)
build.py    data/fixtures.json + template.html -> calendar.html
run.sh      both of the above, with logging, for local/launchd use
```

`calendar.html` is one self-contained file with the data embedded. It opens from
disk, from Pages, or from an emailed copy, with no server and no network.

### The data source

ESPN's public cricket JSON, `site.api.espn.com/apis/site/v2/sports/cricket/<id>/scoreboard`.
No key. It is undocumented and it does **not** behave like the soccer feed the
football project uses:

| | Soccer feed | Cricket feed |
|---|---|---|
| Competition id | slug, e.g. `eng.1` | opaque integer, e.g. `8623` |
| Date query | `?dates=YYYYMMDD-YYYYMMDD` | **404s.** Only `?dates=YYYY` works |
| Omitting dates | current window | silently returns **one** match |
| Competition list | stable and enumerable | none — no directory endpoint exists |

That last row is the real constraint. Bilateral tours get a **fresh league id
every time they are played** — "Bangladesh A tour of South Africa 2026" is id
24694 and will never be that id again — so the full set of competitions cannot
be configured in advance. The pipeline handles this in three layers:

1. **`config.json`** — the recurring competitions (IPL, CPL, Big Bash, ICC events
   and so on) whose ids are stable across seasons.
2. **`discover()`** — reads ESPN's own header feed for whatever is live *right
   now* and picks up tours nobody could have configured.
3. **`data/registry.json`** — remembers every tournament ever discovered, so a
   tour stays on the chart after it drops off ESPN's live board.

The registry is committed on purpose: it is the pipeline's memory, and the chart
gets more complete every day it runs.

### Safety rails

`sync.py` will not publish bad data. It keeps the previous snapshot if the whole
pull fails, refuses to overwrite when a pull returns under 60% of the previous
fixture count (a gutted fetch, not a quiet cricket season), and records per-feed
failures in the payload so the page can say so rather than quietly show less.

`sync.py` also diffs against the previous snapshot, so reschedules,
cancellations and newly listed matches surface at the top of the page.

## The page

Three views over the same filtered set:

- **Season** — a Gantt of every tournament across the window. Bar length is how
  long it runs, the number on the right is how many matches. This is the view
  that answers "what is on in September, and for how long".
- **Months** — calendar grid, one dot per match, coloured by tournament.
- **Matches** — the chronological list, with venue, format and result.

Filter by group, by format (Test / ODI / T20), by text, or narrow to India and
ICC fixtures only.

## Running it

```bash
python3 sync.py     # pull fixtures       (stdlib only, no dependencies)
python3 build.py    # rebuild the page
open calendar.html
```

`./run.sh --scheduled` is the cron-safe form: it no-ops if the data is already
fresher than four hours, so overlapping triggers are harmless.

## Refresh

A GitHub Action re-syncs four times a day — after the Australian, subcontinental,
European and Caribbean windows — commits the snapshot, and republishes the page
to GitHub Pages. Nothing depends on a laptop being switched on.

## Configuration

`config.json`:

- `window_start` / `window_end` — the calendar's date range. Fixtures outside it
  are dropped after the pull.
- `seasons` — which ESPN season-years to request per competition. A tournament
  spanning a new year (Big Bash, SA20) appears under both, and `sync.py`
  de-duplicates by match id.
- `tournaments` — the recurring competitions, with `group` and `color` used by
  the page.

Anything ESPN reports as live that is not in `config.json` arrives on its own,
grouped as `Discovered`, and is remembered from then on.
