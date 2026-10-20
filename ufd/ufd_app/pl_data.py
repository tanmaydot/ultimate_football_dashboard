"""Collect Premier League team data from football-data.org, PitchAPI and footballdata.io.

Run it with ``python manage.py build_pl_data``. It writes one JSON file
(``ufd_app/data/pl_teams.json``) that the dashboard reads, so the dashboard
itself never calls the APIs and never hits their rate limits.

Spine of the data: the 20 teams football-data.org lists for the competition.
PitchAPI and footballdata.io teams are matched onto them by name, and
anything that can't be matched confidently is reported instead of guessed.

Where each part comes from, and when it is fetched again (the build only updates what can have changed):
  football-data.org  squads, coaches, standings, results   every run (4 cheap calls)
  PitchAPI           formations, XIs, possession, player   a match is read when it finishes, for a few days after
                     season numbers                        (corrections), and when its stored score no longer
                                                           matches the league list; nothing else is requested
  footballdata.io    xG, shots, corners ... per club       a club's stats are fetched again only after it has
                                                           played, or after a day; the club list weekly

Each team in the output has:
  profile  merged fields (name, venue, coach, squad ...) the dashboard needs
  stats    season numbers (standings, clean sheets, average possession)
  formation  the formation the manager has used most this season
  raw      the complete, untouched team objects from the APIs

API keys are never written to the output or the logs.
"""
import logging
import os
import re
import time
from collections import Counter, defaultdict
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path

import requests
from django.conf import settings

from . import pitchapi_source
from .footballdata_io import FootballDataIoError, FootballDataIoSource, clean_name  # noqa: F401 (the build command and tests use pl_data.FootballDataIoSource)
from .pitchapi import PitchApiAuthError, PitchApiError
from .util import Throttle, fold, to_int, utc_now_iso, write_json_atomic  # noqa: F401 (pl_data.write_json_atomic is used by the build command)

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
DEFAULT_OUTPUT = Path(__file__).resolve().parent / "data" / "pl_teams.json"

FD_BASE = "https://api.football-data.org/v4"
COMPETITION = "PL"
FD_MIN_INTERVAL = 6.5  # the free plan allows 10 calls a minute
IO_DIRECTORY_MAX_AGE_HOURS = 24 * 7  # footballdata.io's league / season / club list hardly ever changes
MIN_TEAMS = 18  # the league has 20; fewer than this means something went wrong


class FetchError(Exception):
    """A data source could not be read. The message never contains an API key."""


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _to_float(value):
    try:
        return float(str(value).strip().rstrip("%"))
    except (TypeError, ValueError):
        return None


def player_numbers(player):
    """A player's season numbers (the PitchAPI record), an empty dict when there are none."""
    return (player or {}).get("pitchapi") or {}


# --------------------------------------------------------------------------- #
# API clients
# --------------------------------------------------------------------------- #
class FootballDataClient:
    """football-data.org v4 with polite throttling and one retry after a 429."""

    def __init__(self, api_key=None, min_interval=FD_MIN_INTERVAL, sleep=time.sleep, clock=time.monotonic):
        self.api_key = api_key or getattr(settings, "FOOTBALL_DATA_API_KEY", "") or os.environ.get("FOOTBALL_DATA_API_KEY", "")
        self.throttle = Throttle(min_interval, sleep, clock)

    def is_configured(self):
        return bool(self.api_key)

    @staticmethod
    def _retry_after(response):
        raw = response.headers.get("Retry-After") or response.headers.get("X-RequestCounter-Reset") or 60
        seconds = to_int(raw)
        return min(max(seconds if seconds else 60, 1), 120)

    def get(self, path, params=None):
        if not self.api_key:
            raise FetchError("football-data.org key is not set (FOOTBALL_DATA_API_KEY)")
        response = None
        for attempt in (1, 2):
            self.throttle.wait()
            try:
                response = requests.get(
                    FD_BASE + path, headers={"X-Auth-Token": self.api_key}, params=params, timeout=30
                )
            except requests.exceptions.RequestException as exc:
                raise FetchError(f"football-data.org {path}: request failed ({type(exc).__name__})")
            self.throttle.mark()
            if response.status_code == 429 and attempt == 1:
                self.throttle.sleep(self._retry_after(response))
                continue
            break

        status = response.status_code
        if status == 403:
            raise FetchError(f"football-data.org {path}: 403 (key invalid, or this resource isn't in your plan)")
        if status == 429:
            raise FetchError(f"football-data.org {path}: rate limit still exceeded after waiting")
        if status != 200:
            raise FetchError(f"football-data.org {path}: HTTP {status}")
        try:
            return response.json()
        except ValueError:
            raise FetchError(f"football-data.org {path}: response was not valid JSON")


# --------------------------------------------------------------------------- #
# Matching teams across the APIs
# --------------------------------------------------------------------------- #
_STOP_WORDS = {"fc", "afc", "cf", "and"}
_ALIASES = {
    "man city": "manchester city",
    "man utd": "manchester united",
    "man united": "manchester united",
    "manchester utd": "manchester united",
    "newcastle": "newcastle united",
    "newcastle utd": "newcastle united",
    "nottm forest": "nottingham forest",
    "nott m forest": "nottingham forest",
    "wolves": "wolverhampton wanderers",
    "spurs": "tottenham hotspur",
    "tottenham": "tottenham hotspur",
    "brighton": "brighton hove albion",
    "leeds": "leeds united",
    "west ham": "west ham united",
}


def normalise_team_name(name):
    text = fold(name).replace("&", " ")
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    key = " ".join(t for t in text.split() if t not in _STOP_WORDS)
    return _ALIASES.get(key, key)


def match_teams(fd_teams, other_teams):
    """Pair football-data teams with another source's teams (each a dict with a ``team_name``).

    Returns (matches, unmatched_fd, unmatched_other). ``matches`` maps the
    football-data team id to ``(other team, how)``. Exact normalised-name
    matches go first; a fuzzy match is accepted only when it is clearly the
    best candidate. Anything less certain is left unmatched on purpose.
    """
    index = {}
    for team in other_teams:
        index.setdefault(normalise_team_name(team.get("team_name")), team)

    matches, used = {}, set()
    for team in fd_teams:
        for label in ("name", "shortName"):
            key = normalise_team_name(team.get(label))
            if key and key in index and key not in used:
                matches[team["id"]] = (index[key], "exact")
                used.add(key)
                break

    for team in fd_teams:
        if team["id"] in matches:
            continue
        key = normalise_team_name(team.get("name"))
        scored = sorted(
            ((SequenceMatcher(None, key, other).ratio(), other) for other in index if other not in used),
            reverse=True,
        )
        if scored and scored[0][0] >= 0.86 and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.05):
            matches[team["id"]] = (index[scored[0][1]], "fuzzy")
            used.add(scored[0][1])

    unmatched_fd = [t.get("name") for t in fd_teams if t["id"] not in matches]
    unmatched_other = [t.get("team_name") for t in other_teams if normalise_team_name(t.get("team_name")) not in used]
    return matches, unmatched_fd, unmatched_other


# --------------------------------------------------------------------------- #
# football-data derived numbers
# --------------------------------------------------------------------------- #
def extract_standings(data):
    table = []
    for block in (data or {}).get("standings") or []:
        if block.get("type") == "TOTAL":
            table = block.get("table") or []
            break
    rows = []
    for row in table:
        team = row.get("team") or {}
        rows.append({
            "position": row.get("position"),
            "team_id": team.get("id"),
            "name": team.get("name"),
            "short_name": team.get("shortName"),
            "tla": team.get("tla"),
            "crest": team.get("crest"),
            "played": row.get("playedGames", 0),
            "won": row.get("won", 0),
            "draw": row.get("draw", 0),
            "lost": row.get("lost", 0),
            "goals_for": row.get("goalsFor", 0),
            "goals_against": row.get("goalsAgainst", 0),
            "goal_difference": row.get("goalDifference", 0),
            "points": row.get("points", 0),
            "form": row.get("form"),
        })
    return rows


def count_clean_sheets(matches):
    """{team_id: clean sheets} over finished matches that have a full-time score."""
    sheets = defaultdict(int)
    for match in matches or []:
        full_time = (match.get("score") or {}).get("fullTime") or {}
        home, away = full_time.get("home"), full_time.get("away")
        if home is None or away is None:
            continue
        home_id = (match.get("homeTeam") or {}).get("id")
        away_id = (match.get("awayTeam") or {}).get("id")
        sheets[home_id] += away == 0
        sheets[away_id] += home == 0
    return dict(sheets)


def premier_league_titles(competition, team_id):
    """Titles counted over the seasons the API lists, or None if it lists none."""
    years, titles = [], 0
    for season in (competition or {}).get("seasons") or []:
        start = (season.get("startDate") or "")[:4]
        if start.isdigit():
            years.append(int(start))
        if (season.get("winner") or {}).get("id") == team_id:
            titles += 1
    return {"count": titles, "since": min(years)} if years else None


# --------------------------------------------------------------------------- #
# Numbers derived from the stored matches (formation, possession)
# --------------------------------------------------------------------------- #
def _is_finished(event):
    status = (event.get("match_status") or "").strip().lower()
    return status.startswith(("finished", "after")) or status == "ft"


def _starting_xi(side):
    """Names of the starting XI in position order, or None if the lineup isn't usable."""
    players = ((side or {}).get("starting_lineups")) or []
    if len(players) != 11:
        return None
    ordered = []
    for player in players:
        position = to_int(player.get("lineup_position"))
        if position is None or not player.get("lineup_player"):
            return None
        ordered.append((position, player["lineup_player"]))
    if len({p for p, _ in ordered}) != 11:
        return None
    return [name for _, name in sorted(ordered)]


def derive_from_events(events):
    """Per team: formation usage, latest XI per formation, possession.

    Returns {team_key: {"usage": Counter, "latest": {formation: {...}}, "possession": [floats]}}
    """
    teams = defaultdict(lambda: {"usage": Counter(), "latest": {}, "possession": []})
    seen = set()
    for event in events or []:
        if not isinstance(event, dict) or not _is_finished(event):
            continue
        match_id = event.get("match_id")
        if match_id in seen:
            continue
        seen.add(match_id)

        possession = next(
            (s for s in event.get("statistics") or [] if (s.get("type") or "").lower() == "ball possession"), None
        )
        for side in ("home", "away"):
            team_key = event.get(f"match_{side}team_id")
            if not team_key:
                continue
            bucket = teams[str(team_key)]
            formation = (event.get(f"match_{side}team_system") or "").strip()
            if formation:
                bucket["usage"][formation] += 1
                previous = bucket["latest"].get(formation)
                when = event.get("match_date") or ""
                if previous is None or when >= previous["date"]:
                    bucket["latest"][formation] = {
                        "date": when,
                        "starting_xi": _starting_xi((event.get("lineup") or {}).get(side)),
                    }
            if possession:
                value = _to_float(possession.get(side))
                if value is not None:
                    bucket["possession"].append(value)
    return teams


def formation_summary(derived):
    """The formation of the latest match, plus the most-used one (ties go to the most recently used) with its latest XI."""
    usage = derived["usage"]
    if not usage:
        return {"default": None, "usage": {}, "matches_counted": 0, "starting_xi": None, "based_on_match_date": None,
                "last_used": None, "last_used_xi": None, "last_used_date": None, "formations": []}
    top = max(usage.values())
    contenders = [f for f, n in usage.items() if n == top]
    default = max(contenders, key=lambda f: derived["latest"].get(f, {}).get("date", ""))
    latest = derived["latest"].get(default, {})
    # The formation of the most recent match (each formation remembers the date and XI of its latest use)
    last_used, last = max(derived["latest"].items(), key=lambda item: item[1]["date"])
    # Every formation tried, most used first (ties: most recent), each with the XI from its latest match
    formations = [
        {"formation": name, "count": count, "last_date": derived["latest"].get(name, {}).get("date") or None,
         "starting_xi": derived["latest"].get(name, {}).get("starting_xi")}
        for name, count in usage.items()
    ]
    formations.sort(key=lambda f: (f["count"], f["last_date"] or ""), reverse=True)
    return {
        "formations": formations,
        "last_used": last_used,
        "last_used_xi": last.get("starting_xi"),
        "last_used_date": last.get("date") or None,
        "default": default,
        "usage": dict(usage.most_common()),
        "matches_counted": sum(usage.values()),
        "starting_xi": latest.get("starting_xi"),
        "based_on_match_date": latest.get("date") or None,
    }


# --------------------------------------------------------------------------- #
# Merging one team
# --------------------------------------------------------------------------- #
def _person_key(name):
    return re.sub(r"[^a-z ]+", " ", fold(name)).split()


def attach_pitch_stats(squad, pitch_players, pitch_played):
    return pitchapi_source.attach_pitch_stats(squad, pitch_players, pitch_played)


def _pitch_pool(totals, info, team_id):
    """{PitchAPI player id: name, image and season totals} for one club."""
    return {pid: {"name": (info.get(pid) or {}).get("name"), "image": (info.get(pid) or {}).get("image"), "total": total}
            for pid, total in totals.items() if total.get("team") == team_id}


def build_squad(fd_squad):
    """The football-data squad as the dashboard lists it (season numbers are attached from PitchAPI afterwards)."""
    return [{
        "id": player.get("id"),
        "name": player.get("name"),
        "position": player.get("position"),
        "nationality": player.get("nationality"),
        "date_of_birth": player.get("dateOfBirth"),
        "shirt_number": player.get("shirtNumber"),
    } for player in fd_squad or []]


def _positive(value):
    """footballdata.io uses 0 / -1 / null for "no data"; only a real positive number counts."""
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else None


def extract_io_stats(data):
    """The footballdata.io /teams/{id}/stats numbers worth keeping (all None when there is no data)."""
    data = data if isinstance(data, dict) else {}
    played = _positive((data.get("summary") or {}).get("matches_played"))

    def per_match(block, key, recorded_key=None):
        block = block or {}
        if recorded_key and not _positive(block.get(recorded_key)):
            return None
        return _positive(block.get(key))

    return {
        "matches": played,
        "possession": _positive((data.get("possession") or {}).get("average")) if played else None,
        "clean_sheets": (data.get("clean_sheets") or {}).get("total") if played else None,
        "xg_for": _positive((data.get("xg") or {}).get("xg_for")),
        "shots_per_match": per_match(data.get("shots"), "shots_per_match", "recorded_matches"),
        "shots_on_target_per_match": per_match(data.get("shots"), "shots_on_target_per_match", "recorded_matches"),
        "corners_per_match": per_match(data.get("corners"), "for_per_match", "recorded_matches"),
        "fouls_per_match": per_match(data.get("fouls"), "committed_per_match"),
        "cards_per_match": per_match(data.get("cards"), "for_per_match"),
    }


def build_team(fd_team, stats_row, clean_sheets, derived, titles, io_team=None, io_raw_stats=None,
               pitch_team=None, pitch_players=None, pitch_played=0, pitch_coach=None):
    io_team = io_team or {}
    io_stats = extract_io_stats(io_raw_stats)

    coach = fd_team.get("coach") or None
    if coach:
        coach = {
            "name": coach.get("name") or " ".join(filter(None, [coach.get("firstName"), coach.get("lastName")])) or None,
            "nationality": coach.get("nationality"),
            "date_of_birth": coach.get("dateOfBirth"),
            "contract": coach.get("contract"),
        }
    if not (coach and coach.get("name")) and pitch_coach:
        coach = {"name": pitch_coach, "nationality": None, "date_of_birth": None, "contract": None}

    staff = [{"name": s.get("name"), "role": s.get("role") or s.get("position")}
             for s in fd_team.get("staff") or [] if isinstance(s, dict) and s.get("name")]

    possession = derived["possession"] if derived else []
    if possession:
        possession_value = round(sum(possession) / len(possession), 1)
        possession_source, possession_matches = derived.get("source", "pitchapi"), len(possession)
    elif io_stats["possession"]:
        possession_value, possession_source, possession_matches = round(io_stats["possession"], 1), "footballdata.io", io_stats["matches"]
    else:
        possession_value, possession_source, possession_matches = None, None, 0
    stats_row = stats_row or {}
    played = stats_row.get("played") or 0
    points = stats_row.get("points") or 0
    stats = {
        "position": stats_row.get("position"),
        "played": played,
        "won": stats_row.get("won"),
        "draw": stats_row.get("draw"),
        "lost": stats_row.get("lost"),
        "goals_for": stats_row.get("goals_for"),
        "goals_against": stats_row.get("goals_against"),
        "goal_difference": stats_row.get("goal_difference"),
        "points": points,
        "avg_points": round(points / played, 2) if played else None,
        "form": stats_row.get("form"),
        "clean_sheets": clean_sheets if clean_sheets is not None else io_stats["clean_sheets"],
        "possession": possession_value,
        "possession_matches": possession_matches,
        "possession_source": possession_source,
        # footballdata.io only: depth the other sources don't have
        "xg_for": io_stats["xg_for"],
        "shots_per_match": io_stats["shots_per_match"],
        "shots_on_target_per_match": io_stats["shots_on_target_per_match"],
        "corners_per_match": io_stats["corners_per_match"],
        "fouls_per_match": io_stats["fouls_per_match"],
        "cards_per_match": io_stats["cards_per_match"],
    }

    squad = build_squad(fd_team.get("squad"))
    pitch_unmatched = attach_pitch_stats(squad, pitch_players, pitch_played) if pitch_players is not None else []

    profile = {
        "id": fd_team["id"],
        "name": fd_team.get("name"),
        "short_name": fd_team.get("shortName"),
        "tla": fd_team.get("tla"),
        "crest": fd_team.get("crest") or io_team.get("team_logo"),
        "founded": fd_team.get("founded") or to_int(io_team.get("founded")),
        "club_colors": fd_team.get("clubColors"),
        "website": fd_team.get("website"),
        "address": fd_team.get("address"),
        "country": (fd_team.get("area") or {}).get("name"),
        "market_value": fd_team.get("marketValue"),
        # football-data only knows the stadium's name; capacity, city and so on come from club_extras (see dashboard.py)
        "venue": {"name": fd_team.get("venue"), "city": None, "address": None, "capacity": None, "surface": None},
        "coach": coach,
        "staff": staff,
        "squad": squad,
        "running_competitions": [c.get("name") for c in fd_team.get("runningCompetitions") or []],
        "premier_league_titles": titles,
    }
    return {
        "profile": profile,
        "stats": stats,
        "formation": formation_summary(derived) if derived else formation_summary({"usage": Counter(), "latest": {}}),
        "sources": {
            "football_data": True,
            "footballdata_io": bool(io_team),
            "footballdata_io_team_id": io_team.get("team_id"),
            "pitchapi": bool(pitch_team),
            "pitchapi_team_id": (pitch_team or {}).get("id"),
            "pitchapi_unmatched_players": pitch_unmatched,
        },
        "raw": {
            "football_data": fd_team,
            "footballdata_io": {"team": io_team, "stats": io_raw_stats} if io_team else None,
        },
    }


# --------------------------------------------------------------------------- #
# The whole build
# --------------------------------------------------------------------------- #
def build_dataset(fd, pa=None, io=None, history=None, log=print, today=None, io_max_age_hours=24, now=None, force=False):
    """Fetch what has changed and return the dataset dict. Raises FetchError if the 20-team spine can't be built.

    ``force`` ignores everything the history remembers and fetches it all again.
    """
    today = today or date.today()
    report = {"stages": [], "unmatched_footballdata_io": [], "unmatched_pitchapi": [], "unmatched_pitchapi_players": [],
              "pitchapi": None, "updates": {}}
    updates = report["updates"]

    def stage(name, ok, detail=""):
        report["stages"].append({"name": name, "ok": ok, "detail": detail})
        log(f"  [{'ok' if ok else 'skipped'}] {name}" + (f": {detail}" if detail else ""))

    # --- football-data: the spine (always: 4 cheap calls) -------------------------
    log("football-data.org (about 4 calls, throttled to the free-plan limit)")
    competition = fd.get(f"/competitions/{COMPETITION}")
    stage("competition + seasons", True)
    if history is not None:
        history.start_season(((competition.get("currentSeason") or {}).get("startDate") or "")[:10] or None)
        for note in history.notes:
            log(f"  [history] {note}")
    teams_payload = fd.get(f"/competitions/{COMPETITION}/teams")
    fd_teams = [t for t in teams_payload.get("teams") or [] if isinstance(t, dict) and t.get("id")]
    if len(fd_teams) < MIN_TEAMS:
        raise FetchError(f"football-data returned {len(fd_teams)} teams; expected about 20. Nothing was written.")
    stage("teams", True, f"{len(fd_teams)} teams")

    for team in fd_teams:  # the list endpoint normally includes squads; top up from the team endpoint if not
        if not team.get("squad") and not team.get("coach"):
            detail = fd.get(f"/teams/{team['id']}")
            team.update({k: v for k, v in detail.items() if v not in (None, [], "")})
            log(f"    fetched full details for {team.get('name')}")

    standings = extract_standings(fd.get(f"/competitions/{COMPETITION}/standings"))
    stage("standings", bool(standings), f"{len(standings)} rows")
    stats_by_team = {row["team_id"]: row for row in standings}

    try:
        matches = fd.get(f"/competitions/{COMPETITION}/matches", params={"status": "FINISHED"}).get("matches") or []
        new_results = history.add_results(matches) if history is not None else 0
        clean_sheets = count_clean_sheets(matches)
        updates["new_results"] = new_results
        stage("finished matches (clean sheets)", True,
              f"{len(matches)} matches" + (f", {new_results} new since the last build" if history is not None else ""))
    except FetchError as exc:
        stored = history.results_as_matches() if history is not None else []
        clean_sheets = count_clean_sheets(stored) if stored else {}
        stage("finished matches (clean sheets)", bool(stored), f"{exc}" + (f"; used {len(stored)} stored results" if stored else ""))

    # --- PitchAPI: formations, lineups, possession, player season numbers -------------
    pitch_by_team, pitch_derived, pitch_totals, pitch_info, pitch_coaches = {}, {}, {}, {}, {}
    pitch_played = defaultdict(int)
    from .history import History  # local import: history imports this module when saving
    store = history if history is not None else History(path=None)
    teams_list = None
    if pa is None or not pa.is_configured():
        stage("pitchapi", False, "PITCHAPI_API_KEY is not set"
              + (f"; using the {len(store.pitch_matches)} matches stored earlier" if store.pitch_matches
                 else "; formations, possession and player numbers will be empty"))
    else:
        log("PitchAPI")
        pa.use_league(store.meta.get("pitchapi_league_id"))
        try:
            teams_list = pa.all_teams(minimum=len(fd_teams))
            result = pitchapi_source.refresh_history(pa, store, today=today, log=log, force=force)
            if pa.known_league_id:
                store.meta["pitchapi_league_id"] = pa.known_league_id
            updates["pitchapi_matches"] = {k: result[k] for k in ("finished", "new", "refreshed", "unchanged")}
            reasons = ", ".join(f"{n} {why}" for why, n in sorted(result["reasons"].items()))
            stage("pitchapi matches (lineups, formations, possession, player numbers)",
                  not result["failed"] and bool(store.pitch_matches),
                  f"{result['finished']} finished: {result['new']} new, {result['refreshed']} read again"
                  + (f" ({reasons})" if result["refreshed"] and reasons else "")
                  + f", {result['unchanged']} unchanged and not requested"
                  + (f"; failed: {', '.join(result['failed'][:5])}" if result["failed"] else ""))
        except (PitchApiError, PitchApiAuthError) as exc:
            stage("pitchapi", False, str(exc) + ("; using the matches stored earlier" if store.pitch_matches else ""))
    if store.pitch_matches:
        records = store.pitch_records()
        # The stored matches name both clubs of every match, so a club is found even when the API is down or lists nothing
        known = {t["id"] for t in teams_list or []}
        teams_list = list(teams_list or []) + [t for t in pitchapi_source.teams_from_records(records) if t["id"] not in known]
        candidates = [{"team_name": t["name"], "team_key": t["id"], "pitch": t} for t in teams_list]
        found, report["unmatched_pitchapi"], _ = match_teams(fd_teams, candidates)
        pitch_by_team = {fd_id: cand["pitch"] for fd_id, (cand, _how) in found.items()}
        stage("pitchapi team matching", not report["unmatched_pitchapi"],
              f"{len(pitch_by_team)}/{len(fd_teams)} matched"
              + (f"; unmatched: {', '.join(report['unmatched_pitchapi'])}" if report["unmatched_pitchapi"] else ""))
        pitch_derived = derive_from_events([r["event"] for r in records])
        for derived_team in pitch_derived.values():
            derived_team["source"] = "pitchapi"
        pitch_totals = pitchapi_source.aggregate_players(records)
        pitch_info = store.data["pitch_players"]
        pitch_coaches = pitchapi_source.latest_coaches(records)
        for record in records:
            for side in ("home", "away"):
                pitch_played[record["event"].get(f"match_{side}team_id")] += 1
        off = sum(1 for r in records if r.get("cards_ok") is False)
        report["pitchapi"] = {"matches": len(records), "cards_not_reconciled": off}
        if off:
            stage("pitchapi cards", False,
                  f"in {off} of {len(records)} matches the cards don't add up to the team statistics (counts can be off by one or two)")
    pitch_ok = bool(pitch_derived)

    # --- footballdata.io: xG, shots, corners (about 23 of the 2,000 monthly requests, and only after a club has played) --
    io_by_team, io_stats_by_team = {}, {}
    if io is None or not io.is_configured():
        stage("footballdata.io", False, "FOOTBALLDATA_IO_API_KEY is not set (or skipped by option)")
    else:
        log("footballdata.io")
        try:
            directory = None if force or history is None else history.io_directory(IO_DIRECTORY_MAX_AGE_HOURS, now)
            if directory:
                io_league_id, season_id, io_teams = directory["league_id"], directory["season_id"], directory["teams"]
            else:
                io_league_id = io.premier_league_id()
                season_id, io_teams = io.get_current_teams(io_league_id) if io_league_id else (None, [])
                if io_teams and history is not None:
                    history.set_io_directory(io_league_id, season_id, io_teams, now)
            if not io_teams:
                stage("footballdata.io teams", False, "Premier League teams not found")
            else:
                candidates = [{"team_name": clean_name(t.get("team_name_clean") or t.get("team_name")), "io": t} for t in io_teams]
                io_matches, report["unmatched_footballdata_io"], _ = match_teams(fd_teams, candidates)
                io_by_team = {fd_id: cand["io"] for fd_id, (cand, _how) in io_matches.items()}
                stage("footballdata.io team matching", not report["unmatched_footballdata_io"],
                      f"{len(io_by_team)}/{len(fd_teams)} matched"
                      + (" (club list from the last week)" if directory else "")
                      + (f"; unmatched: {', '.join(report['unmatched_footballdata_io'])}" if report["unmatched_footballdata_io"] else ""))
                missing, reused, fetched = [], 0, 0
                for fd_id, io_team in io_by_team.items():
                    played = (stats_by_team.get(fd_id) or {}).get("played")
                    cached = None if force or history is None else history.io_cached(fd_id, io_max_age_hours, now, played)
                    if cached:
                        io_stats_by_team[fd_id] = cached
                        reused += 1
                        continue
                    try:
                        stats = io.get_team_stats(io_team["team_id"], io_league_id, season_id)
                    except FootballDataIoError as exc:
                        if exc.status in (401, 403, 429):
                            raise
                        stats = None
                    if stats:
                        io_stats_by_team[fd_id] = stats
                        fetched += 1
                        if history is not None:
                            history.set_io(fd_id, stats, now, played)
                    else:
                        missing.append(io_team.get("team_name"))
                updates["footballdata_io"] = {"fetched": fetched, "reused": reused}
                stage("footballdata.io team stats", bool(io_stats_by_team),
                      f"{len(io_stats_by_team)}/{len(io_by_team)} teams: {fetched} fetched"
                      + (f", {reused} unchanged since the club last played (not requested)" if reused else "")
                      + (f"; none for: {', '.join(missing)}" if missing else ""))
        except FootballDataIoError as exc:
            stage("footballdata.io", False, str(exc))

    # --- assemble ------------------------------------------------------------
    teams = {}
    for fd_team in fd_teams:
        pitch_team = pitch_by_team.get(fd_team["id"])
        team_derived = pitch_derived.get(pitch_team["id"]) if pitch_ok and pitch_team else None
        teams[str(fd_team["id"])] = build_team(
            fd_team, stats_by_team.get(fd_team["id"]),
            clean_sheets.get(fd_team["id"], 0 if clean_sheets else None), team_derived,
            premier_league_titles(competition, fd_team["id"]),
            io_team=io_by_team.get(fd_team["id"]), io_raw_stats=io_stats_by_team.get(fd_team["id"]),
            pitch_team=pitch_team,
            pitch_players=_pitch_pool(pitch_totals, pitch_info, pitch_team["id"]) if pitch_ok and pitch_team else None,
            pitch_played=pitch_played.get(pitch_team["id"], 0) if pitch_team else 0,
            pitch_coach=pitch_coaches.get(pitch_team["id"]) if pitch_team else None,
        )
    if pitch_ok:
        report["unmatched_pitchapi_players"] = [
            f"{team['profile']['name']}: {name}" for team in teams.values()
            for name in team["sources"]["pitchapi_unmatched_players"]]
        if report["unmatched_pitchapi_players"]:
            stage("pitchapi players", False, f"{len(report['unmatched_pitchapi_players'])} players who appeared match nobody in the squad lists: "
                  + ", ".join(report["unmatched_pitchapi_players"][:6]))

    if history is not None:
        day = today.isoformat()
        for team_id, team in teams.items():
            st = team.get("stats") or {}
            history.record_team(team_id, day, {k: st.get(k) for k in (
                "position", "played", "won", "draw", "lost", "points", "goals_for", "goals_against",
                "goal_difference", "clean_sheets", "possession", "form")})
            for player in (team.get("profile") or {}).get("squad") or []:
                numbers_record = player_numbers(player)
                if player.get("id") is None or not numbers_record:
                    continue
                numbers = {k: numbers_record.get(k) for k in (
                    "appearances", "goals", "assists", "yellow_cards", "red_cards", "rating", "injured")}
                numbers.update(numbers_record.get("extra") or {})
                history.record_player(team_id, player["id"], player.get("name"), day, numbers)
        stage("history", True, ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in history.summary().items()))

    parts = []
    if "new_results" in updates:
        parts.append(f"{updates['new_results']} new result(s)")
    if "pitchapi_matches" in updates:
        pm = updates["pitchapi_matches"]
        parts.append(f"PitchAPI {pm['new']} new / {pm['refreshed']} re-read / {pm['unchanged']} untouched")
    if "footballdata_io" in updates:
        parts.append(f"footballdata.io {updates['footballdata_io']['fetched']} club(s) refreshed")
    stage("updated this run", True, "; ".join(parts) if parts else "standings, squads and coaches only")

    season = competition.get("currentSeason") or {}
    return {
        "meta": {
            "schema_version": SCHEMA_VERSION,
            "generated_at": utc_now_iso(),
            "competition": {"code": COMPETITION, "name": competition.get("name")},
            "season": {"start": season.get("startDate"), "end": season.get("endDate"),
                       "current_matchday": season.get("currentMatchday")},
            "report": report,
            "history": history.summary() if history is not None else None,
        },
        "standings": standings,
        "teams": teams,
    }
