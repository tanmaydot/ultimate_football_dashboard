"""A small persistent JSON store for one season of Premier League data.

The free API plans only show the latest few matches, and most of what the dashboard shows only changes when a
match finishes. So each ``build_pl_data`` run keeps what it has seen in this file and only asks for what is new:

* ``pitch_matches`` PitchAPI finished matches, keyed by match id: the match as a flat record (formations, XIs,
                    possession), each player's numbers for that match (sparse), and whether the cards added up to the
                    team statistics. Season totals are summed from these, so they keep growing across runs.
* ``pitch_players`` PitchAPI player id -> name, image and club (stable details, kept once)
* ``results``       football-data finished scores, keyed by match id
* ``team_stats``    per club, one snapshot per day (only when something changed)
* ``player_stats``  per club and player, one snapshot per day (only when something changed)
* ``io_stats``      footballdata.io team stats, with when they were fetched and how many matches the club had
                    played then: they are fetched again only when the club has played since (or after a day)
* ``meta``          small things worth remembering between runs (the PitchAPI league id, footballdata.io's
                    league / season / team list)

A history file written by an older version (it had a ``matches`` section) still loads: that section is dropped.
"""
import logging
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from .util import DATA_DIR, read_json, utc_now, utc_now_iso, write_json_atomic

logger = logging.getLogger(__name__)

HISTORY_VERSION = 1
DEFAULT_HISTORY = DATA_DIR / "pl_history.json"


def _fresh(season_start=None):
    return {"version": HISTORY_VERSION, "season_start": season_start, "updated_at": None,
            "results": {}, "team_stats": {}, "player_stats": {}, "io_stats": {},
            "pitch_matches": {}, "pitch_players": {}, "meta": {}}


class History:
    def __init__(self, path=DEFAULT_HISTORY, data=None):
        self.path = Path(path) if path else None
        self.data = data if data is not None else _fresh()
        self.notes = []  # what happened while loading (shown by the command)

    # ---- load / save ------------------------------------------------------
    @classmethod
    def load(cls, path=DEFAULT_HISTORY):
        history = cls(path)
        if not history.path or not history.path.exists():
            return history
        try:
            data = read_json(history.path)
            if not isinstance(data, dict) or data.get("version") != HISTORY_VERSION:
                raise ValueError("unknown history format")
            for key, empty in _fresh().items():
                data.setdefault(key, empty)
            dropped = len(data.pop("matches", None) or {})  # the section older versions kept: no longer used
            if dropped:
                history.notes.append(f"Dropped {dropped} matches stored by an older version (PitchAPI matches are used now).")
            history.data = data
        except (OSError, ValueError) as exc:
            backup = history.path.with_suffix(".corrupt.json")
            try:
                shutil.copyfile(history.path, backup)
            except OSError:
                pass
            history.notes.append(f"History file could not be read ({exc}); started a new one (old copy: {backup.name}).")
        return history

    def save(self):
        if not self.path:
            return
        self.data["updated_at"] = utc_now_iso()
        write_json_atomic(self.path, self.data)

    def start_season(self, season_start):
        """One season per file: a different season start means the old data no longer applies."""
        known = self.data.get("season_start")
        if known and season_start and known != season_start:
            if self.path and self.path.exists():
                archive = self.path.with_name(f"{self.path.stem}.{known}.json")
                try:
                    shutil.copyfile(self.path, archive)
                    self.notes.append(f"New season: the previous history was kept as {archive.name}.")
                except OSError:
                    pass
            self.data = _fresh(season_start)
        elif season_start:
            self.data["season_start"] = season_start

    # ---- PitchAPI matches --------------------------------------------------
    @property
    def pitch_matches(self):
        return self.data["pitch_matches"]

    def pitch_match(self, match_id):
        return self.pitch_matches.get(str(match_id))

    def add_pitch_match(self, match_id, record):
        """Store one finished match (replacing an older copy); the player details are kept once, newest wins."""
        record = dict(record)
        for pid, info in (record.pop("info", None) or {}).items():
            self.data["pitch_players"][pid] = {k: v for k, v in info.items() if v}
        self.pitch_matches[str(match_id)] = record

    def last_match_date(self):
        dates = [(r.get("event") or {}).get("match_date") for r in self.pitch_matches.values()]
        dates = [d for d in dates if d]
        return max(dates) if dates else None

    def pitch_records(self):
        """Stored PitchAPI matches, oldest first (so 'latest' values such as shirt numbers end up newest)."""
        return sorted(self.pitch_matches.values(), key=lambda r: ((r.get("event") or {}).get("match_date") or "",
                                                                   (r.get("event") or {}).get("match_time") or ""))

    # ---- football-data results ---------------------------------------------
    def add_results(self, matches):
        added = 0
        for m in matches or []:
            if not isinstance(m, dict) or not m.get("id"):
                continue
            full = (m.get("score") or {}).get("fullTime") or {}
            record = {"date": (m.get("utcDate") or "")[:10], "home": (m.get("homeTeam") or {}).get("id"),
                      "away": (m.get("awayTeam") or {}).get("id"), "home_goals": full.get("home"),
                      "away_goals": full.get("away"), "matchday": m.get("matchday")}
            if self.data["results"].get(str(m["id"])) != record:
                added += 1 if str(m["id"]) not in self.data["results"] else 0
                self.data["results"][str(m["id"])] = record
        return added

    def results_as_matches(self):
        """Stored results in football-data's shape, for when the live call fails (clean sheets keep working)."""
        return [{"homeTeam": {"id": r["home"]}, "awayTeam": {"id": r["away"]},
                 "score": {"fullTime": {"home": r["home_goals"], "away": r["away_goals"]}}}
                for r in self.data["results"].values() if r.get("home_goals") is not None]

    # ---- snapshots -----------------------------------------------------------
    @staticmethod
    def _append_snapshot(series, day, values):
        """Append (or, on the same day, replace) a snapshot, but only when the numbers differ from the last one."""
        values = {k: v for k, v in values.items() if v is not None}
        if not values:
            return False
        if series and {k: v for k, v in series[-1].items() if k != "date"} == values:
            return False
        snapshot = {"date": day, **values}
        if series and series[-1].get("date") == day:
            series[-1] = snapshot
        else:
            series.append(snapshot)
        return True

    def record_team(self, team_id, day, values):
        series = self.data["team_stats"].setdefault(str(team_id), [])
        return self._append_snapshot(series, day, values)

    def record_player(self, team_id, player_id, name, day, values):
        club = self.data["player_stats"].setdefault(str(team_id), {})
        entry = club.setdefault(str(player_id), {"name": name, "snapshots": []})
        entry["name"] = name or entry.get("name")
        return self._append_snapshot(entry["snapshots"], day, values)

    # ---- small remembered things -----------------------------------------------
    @property
    def meta(self):
        return self.data["meta"]

    # ---- footballdata.io cache -----------------------------------------------
    def io_cached(self, team_id, max_age_hours, now=None, played=None):
        """Stored stats for a club, unless they are old or the club has played since they were fetched."""
        item = self.data["io_stats"].get(str(team_id))
        if not item or not item.get("data"):
            return None
        try:
            fetched = datetime.fromisoformat(item["fetched_at"])
        except (KeyError, ValueError):
            return None
        if played is not None and item.get("played") is not None and item["played"] != played:
            return None
        return item["data"] if (now or utc_now()) - fetched < timedelta(hours=max_age_hours) else None

    def set_io(self, team_id, data, now=None, played=None):
        self.data["io_stats"][str(team_id)] = {"fetched_at": (now or utc_now()).isoformat(timespec="seconds"),
                                               "played": played, "data": data}

    def io_directory(self, max_age_hours, now=None):
        """footballdata.io's league id, season id and team list, if they were looked up recently."""
        item = self.meta.get("io_directory")
        if not item or not item.get("teams"):
            return None
        try:
            fetched = datetime.fromisoformat(item["fetched_at"])
        except (KeyError, ValueError):
            return None
        return item if (now or utc_now()) - fetched < timedelta(hours=max_age_hours) else None

    def set_io_directory(self, league_id, season_id, teams, now=None):
        self.meta["io_directory"] = {"fetched_at": (now or utc_now()).isoformat(timespec="seconds"),
                                     "league_id": league_id, "season_id": season_id, "teams": teams}

    # ---- summary ---------------------------------------------------------------
    def summary(self):
        return {
            "results_stored": len(self.data["results"]),
            "last_match_date": self.last_match_date(),
            "pitch_matches_stored": len(self.pitch_matches),
            "team_snapshots": sum(len(s) for s in self.data["team_stats"].values()),
            "player_snapshots": sum(len(p["snapshots"]) for club in self.data["player_stats"].values() for p in club.values()),
        }
