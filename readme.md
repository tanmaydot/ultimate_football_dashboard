# ⚽ Ultimate Football Dashboard

A football dashboard built with Django that brings football data, match information, and Premier League season history together in one place.

---

## 🛠️ Tech Stack

- **Backend:** Django (Python)
- **Frontend:** HTML, CSS, and Django Templates
- **API Sources:**
  - [FootballData.io](https://footballdata.io/documentation/)
  - [football-data.org](https://www.football-data.org/)
  - [PitchAPI](https://pitchapi.dev) (formations, lineups, possession, player season numbers)
  - [API-Football](https://apifootball.com/) (venue details only, when PitchAPI is in use)

---

## PitchAPI harvest (formations, lineups, possession, player stats)

`harvest_pitchapi` downloads everything [PitchAPI](https://pitchapi.dev) has for the Premier League and keeps each raw
response as JSON in `ufd/pitchapi_raw/`. It is resumable: stop it any time and run the same command to continue.

```bash
export PITCHAPI_API_KEY="pk_test_..."        # Windows PowerShell:  $env:PITCHAPI_API_KEY="pk_test_..."
cd ufd
python manage.py harvest_pitchapi --seasons 2026/2027,2025/2026 --include-upcoming --zip pitchapi_harvest.zip
# everything the API lists for the league (thousands of requests, hours): add --all-seasons
```

Useful options: `--endpoints lineups,stats,events,players` (only some), `--per-player` (4 extra calls per player per match),
`--max-minutes 60` / `--max-requests 5000` (stop cleanly), `--refresh` (download again). `ufd/pitchapi_raw/_index.json`
summarises what was fetched, what the API said is unavailable, and any failures. The key is never written to disk.

### Using PitchAPI in the daily build

`build_pl_data` now takes formations, starting XIs, possession and every player's season numbers from PitchAPI
(add a `PITCHAPI_API_KEY` secret next to the others). It fetches each finished match once (4 requests: lineups, stats,
players, events), re-fetches the last 3 days in case of corrections, and keeps them in `pl_history.json`; season totals
are summed from those. football-data.org still supplies the squads, coaches and club details, footballdata.io the xG,
and apifootball.com is only asked for venue capacity / city / surface.

```bash
python manage.py build_pl_data                                   # live: uses PITCHAPI_API_KEY
python manage.py build_pl_data --pitchapi-harvest pitchapi_raw   # from a harvest folder, no key or network for PitchAPI
python manage.py build_pl_data --skip-pitchapi                   # the old apifootball path
python manage.py audit_pitchapi pitchapi_raw                     # consistency checks on a harvest
```

If PitchAPI fails, the build uses the matches it stored earlier; with nothing stored it falls back to apifootball.
The build report lists clubs that did not match, players who appeared but are in no squad list, and matches whose
card counts do not add up to the team statistics.
