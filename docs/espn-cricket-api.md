# ESPN's cricket feed: what it does and does not do

Notes from mapping the endpoint this project runs on. It is undocumented and
differs from the soccer feed in ways that matter, so they are written down here
rather than rediscovered.

## Endpoint

```
https://site.api.espn.com/apis/site/v2/sports/cricket/<league_id>/scoreboard
```

No key, no auth. `<league_id>` is an opaque integer, not a slug.

### Query parameters

| Request | Result |
|---|---|
| no params | **one** match — the most recent. Not an error, just one row |
| `?limit=1000` | still one match. `limit` is ignored here |
| `?dates=20260501` | still one match. A day-stamped `dates` does nothing |
| `?dates=20260301-20260701` | **HTTP 404.** Ranges are a soccer-only feature |
| `?dates=2026` | the whole 2026 season. **This is the only useful form** |
| `?season=2026` | identical to `?dates=2026` |

The trap is that the wrong parameter returns `200 OK` with one match rather than
an error, so a naive pull looks like it worked and silently reports a 74-match
tournament as having one fixture.

## There is no league directory

Every obvious route is a dead end:

- `/sports/cricket/scoreboard` → 404
- `/sports/cricket/leagues` → 404
- `sports.core.api.espn.com/v2/sports/cricket/leagues` → 200, `count: 0`, empty
- `/sports/cricket/<id>/teams` → 404

The one directory that does work is the header feed, and it only lists what is
**live right now**:

```
https://site.web.api.espn.com/apis/v2/scoreboard/header?sport=cricket
```

## League ids are per-tour, not per-competition

This is the structural fact the whole pipeline is shaped around.

Recurring competitions keep one id across seasons — the IPL is `8048` for 2026
and for 2027. But **bilateral tours get a fresh id every time they are played**:

```
24227  Afghanistan tour of India 2026
24289  West Indies tour of India 2026/27
24273  England tour of Australia 2026/27
```

Next year's West Indies tour of India will be a different number. So the set of
competitions is not configurable in advance, and `config.json` can only ever
hold the recurring half plus a seeded snapshot of the tours known today.

Ids are allocated chronologically by when the series was registered, which is why
The Hundred Women's Competition (`19663`) sits among 2019/20 series rather than
near other franchise leagues.

### Containers nest

ESPN registers a tour parent *and* a per-format child over the same matches:

```
24289  West Indies tour of India 2026/27      <- parent
24287  West Indies in India T20I Series 2026/27
24288  West Indies in India ODI Series 2026/27
```

Requesting all three double-counts. `sync.py` claims match ids globally and
prefers the parent, so a tour is one wallchart row rather than three.

## Rate limiting is a hard IP ban

Sweeping the id space to build the catalogue above got this machine a
**403 Access Denied** from Akamai — an IP-level block, not a `429` with a
`Retry-After`. It cleared on its own after roughly twenty minutes.

Two consequences, both handled in the code:

1. `sync.py` paces itself (`PAUSE = 0.4s`) and never fans out concurrently.
2. A blocked pull raises rather than returning empty, and `sync.py` refuses to
   overwrite a good snapshot with a gutted one, so a future block degrades to
   "yesterday's data" rather than an empty wallchart.

If the catalogue ever needs re-sweeping, do it slowly and once, and keep the
result — that is what `data/registry.json` is for.

## Useful fields

`events[].competitions[0]` carries the parts worth normalising:

- `class.eventType` — `T20`, `ODI`, `Test`. This is where match format lives.
- `description` — `"3rd T20I"`, `"Final"`, the round label.
- `venue.fullName` / `venue.address.city` / `.country`
- `competitors[].team.displayName` and `.score` (`"161/5 (18/20 ov, target 156)"`)
- `status.type.name` — `STATUS_SCHEDULED`, `STATUS_FINAL`, and cricket-specific
  ones the soccer map has no equivalent for: `STATUS_NO_RESULT`, `STATUS_DRAWN`,
  `STATUS_ABANDONED`, `STATUS_RAIN`.

Dates are stamped `2026-05-31T14:00Z` — **minute precision, no seconds** — which
breaks a `%Y-%m-%dT%H:%M:%SZ` parse. `sync.py` pads it before parsing.
