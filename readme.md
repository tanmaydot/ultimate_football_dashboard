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
  - [OpenFootball England](https://github.com/openfootball/england) (open data, CC0: 26 seasons of results for the match predictions)

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

### How `build_pl_data` keeps the data up to date

The build only fetches what can have changed since the last run, and remembers the rest in `pl_history.json`:

| Data | Source | When it is fetched |
|---|---|---|
| Squads, coaches, standings, results | football-data.org | every run (4 cheap calls) |
| Formations, starting XIs, possession, every player's season numbers | PitchAPI | each match once when it finishes, again for 3 days (corrections), and again if the league list shows a different score than the stored one. Unchanged matches are not requested at all, so a run with no new match is 1 request |
| xG, shots, corners per club | footballdata.io | a club's stats only after it has played (or after 24 h); the club list once a week |
| Stadium capacity, city, honours | `club_extras*.json` | refreshed by the quarterly "Refresh club extras" workflow |

```bash
python manage.py build_pl_data                                   # live: needs FOOTBALL_DATA_API_KEY and PITCHAPI_API_KEY
python manage.py build_pl_data --force                           # ignore what is remembered and fetch everything again
python manage.py build_pl_data --pitchapi-harvest pitchapi_raw   # PitchAPI from a harvest folder: no key or network for it
python manage.py build_pl_data --skip-pitchapi                   # don't call PitchAPI (stored matches are still used)
python manage.py audit_pitchapi pitchapi_raw                     # consistency checks on a harvest
```

If PitchAPI fails, the build uses the matches it stored earlier. The last line of the build output, "updated this run",
says what was refreshed. The build report lists clubs that did not match, players who appeared but are in no squad
list, and matches whose card counts do not add up to the team statistics.

### Match and player predictions

`build_predictions` turns 26 seasons of OpenFootball results plus this season's player lines into win / draw / loss odds for
every remaining fixture and a next-match outlook for each player (shown on the **Predictions** page, the squad and player
pages and a dashboard widget). It needs no API key and runs in the daily workflow after `build_pl_data`.

```bash
python manage.py build_predictions            # download the current OpenFootball season, then build pl_predictions.json
python manage.py backtest_predictions         # score both models on history (about a minute) -> pl_validation.json
```

How it works, how well it did on past seasons, what data is missing and how to improve it: see [explain.md](explain.md).

### Working with the generated data

`ufd/ufd_app/data/pl_teams.json` and `pl_history.json` are written by the daily "Refresh Premier League data"
workflow, which commits them. Building locally and committing them too is what causes merge conflicts. A clean way to work:

```bash
git pull --rebase                      # before you build locally
python ufd/manage.py build_pl_data     # only if you want to look at fresh data locally
git checkout ufd/ufd_app/data/         # ...then throw the local data away instead of committing it
python ufd/manage.py check_data        # is what is on disk sound?
```

If a pull ever reports a conflict in a data file, don't edit it by hand: keep the version from the server
(`git checkout --theirs ufd/ufd_app/data/pl_history.json` while rebasing, `--ours` while merging) and let the next build
bring it up to date. The workflow runs `check_data` before it commits, and `.gitattributes` stops git from writing
conflict markers into these files. `pitchapi_raw/` (the 34 MB harvest) is ignored by git; keep it on disk or zip it.
