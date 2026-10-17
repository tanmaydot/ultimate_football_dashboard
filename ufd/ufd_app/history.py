"""A small persistent JSON store for one season of Premier League data.

The free API plans only show the latest few matches, so each ``build_pl_data`` run adds what it
sees to this file instead of starting from scratch:

* ``matches``       trimmed apifootball match events, keyed by match id (formations, XIs, possession)
* ``results``       football-data finished scores, keyed by match id
* ``team_stats``    per club, one snapshot per day (only when something changed)
* ``player_stats``  per club and player, one snapshot per day (only when something changed)
* ``io_stats``      footballdata.io team stats with the time they were fetched (so they are not re-fetched every run)
* ``pitch_matches`` PitchAPI finished matches, keyed by PitchAPI match id: the match in apifootball's event
                    shape (formations, XIs, possession), each player's numbers for that match (sparse), and
                    whether the cards added up to the team statistics. Season totals are summed from these.
* ``pitch_players`` PitchAPI player id -> name, image and club (stable details, kept once)

Formations, possession and clean sheets are derived from the stored matches, so they keep
growing across runs even when the API only returns the latest few.
"""
import json
import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

HISTORY_VERSION = 1
DEFAULT_HISTORY = Path(__file__).resolve().parent / "data" / "pl_history.json"
OVERLAP_DAYS = 3  # re-fetch a few days before the newest stored match: late score or lineup corrections

_EVENT_KEYS = ("match_id", "match_date", "match_time", "match_status", "match_hometeam_id", "match_awayteam_id",
               "match_hometeam_name", "match_awayteam_name", "match_hometeam_score", "match_awayteam_score",
               "match_hometeam_system", "match_awayteam_system")


def _is_finished(event):
    status = (event.get("match_status") or "").strip().lower()
    return status.startswith(("finished", "after")) or status == "ft"


def trim_event(event):
    """Keep only what the dashboard derives from an apifootball event (a full event is tens of KB)."""
    trimmed = {key: event.get(key) for key in _EVENT_KEYS if event.get(key) not in (None, "")}
    lineup = event.get("lineup") or {}
    trimmed["lineup"] = {
        side: {"starting_lineups": [
            {k: p.get(k) for k in ("lineup_player", "lineup_number", "lineup_position", "player_key") if p.get(k) is not None}
            for p in ((lineup.get(side) or {}).get("starting_lineups") or []) if isinstance(p, dict)]}
        for side in ("home", "away")
    }
    trimmed["statistics"] = [s for s in event.get("statistics") or []
                             if isinstance(s, dict) and (s.get("type") or "").lower() == "ball possession"]
    return trimmed


def _fresh(season_start=None):
    return {"version": HISTORY_VERSION, "season_start": season_start, "updated_at": None,
            "matches": {}, "results": {}, "team_stats": {}, "player_stats": {}, "io_stats": {},
            "pitch_matches": {}, "pitch_players": {}}


def _now():
    return datetime.now(timezone.utc)


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
            data = json.loads(history.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("version") != HISTORY_VERSION:
                raise ValueError("unknown history format")
            for key, empty in _fresh().items():
                data.setdefault(key, empty)
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
        from .pl_data import write_json_atomic  # local import: pl_data imports this module
        if not self.path:
            return
        self.data["updated_at"] = _now().isoformat(timespec="seconds")
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

    # ---- matches (apifootball events) ------------------------------------
    @property
    def matches(self):
        return self.data["matches"]

    def events(self):
        return list(self.matches.values())

    def last_match_date(self):
        dates = [m.get("match_date") for m in self.matches.values() if m.get("match_date") and _is_finished(m)]
        return max(dates) if dates else None

    def fetch_start(self, season_start):
        """First day worth asking apifootball for: just before the newest stored match, else the season start."""
        last = self.last_match_date()
        if not last:
            return season_start
        try:
            from datetime import date
            return max(season_start, date.fromisoformat(last[:10]) - timedelta(days=OVERLAP_DAYS))
        except ValueError:
            return season_start

    def add_events(self, events):
        """Upsert events by match id. Returns (new, updated). A finished record is never replaced by an unfinished one."""
        new = updated = 0
        for event in events or []:
            if not isinstance(event, dict) or not event.get("match_id"):
                continue
            key = str(event["match_id"])
            trimmed = trim_event(event)
            old = self.matches.get(key)
            if old is None:
                self.matches[key] = trimmed
                new += 1
            elif old != trimmed and (_is_finished(trimmed) or not _is_finished(old)):
                self.matches[key] = trimmed
                updated += 1
        return new, updated

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

    # ---- footballdata.io cache -----------------------------------------------
    def io_cached(self, team_id, max_age_hours, now=None):
        item = self.data["io_stats"].get(str(team_id))
        if not item or not item.get("data"):
            return None
        try:
            fetched = datetime.fromisoformat(item["fetched_at"])
        except (KeyError, ValueError):
            return None
        return item["data"] if (now or _now()) - fetched < timedelta(hours=max_age_hours) else None

    def set_io(self, team_id, data, now=None):
        self.data["io_stats"][str(team_id)] = {"fetched_at": (now or _now()).isoformat(timespec="seconds"), "data": data}

    # ---- summary ---------------------------------------------------------------
    def summary(self):
        return {
            "matches_stored": len(self.matches),
            "results_stored": len(self.data["results"]),
            "last_match_date": self.last_match_date(),
            "pitch_matches_stored": len(self.pitch_matches),
            "team_snapshots": sum(len(s) for s in self.data["team_stats"].values()),
            "player_snapshots": sum(len(p["snapshots"]) for club in self.data["player_stats"].values() for p in club.values()),
        }
