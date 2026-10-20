"""PitchAPI as the source of formations, lineups, possession and player season numbers.

The numbers are checked (see ``pitchapi_audit``): possession agrees with the pass share of the same match,
and goals summed over players add up to the real league total.

Only what changed is fetched (see ``refresh_history``): a match is read once when it finishes, again for a few
days in case of corrections, and again whenever its stored score or lineup no longer matches the league list.

Two interchangeable sources feed the build:

* ``PitchApiSource``  calls the live API (1 request for the match list, plus 4 per match that needs reading);
* ``HarvestSource``   reads a folder made by ``manage.py harvest_pitchapi`` (no key, no network).

Both expose ``is_configured()``, ``all_teams()``, ``finished_matches()`` and ``bundle(match_id)``.

Findings from the real data that this module works around:
  * ``/leagues`` lists four leagues called "Premier League" (England, Russia, Egypt, Canada): the English
    one is picked by ``country_code == "ENG"``, never by name alone;
  * an event is sometimes logged twice, one minute apart (a goal, or a yellow card): ``dedupe_events``;
  * yellow / red cards still don't always add up to the match's team statistics, so every stored match
    records whether they did (``cards_ok``) and the build report counts the ones that did not;
  * a player's team and shirt number come from the lineups; there are no birth dates or positions at all;
  * the same coach can be spelled with and without accents ("Jakirovic" / "Jakirović"): ``pick_spelling``.

The API key is never written anywhere (the client in ``pitchapi.py`` only sends it as a header).
"""
import logging
import re
from collections import Counter, defaultdict
from datetime import date, timedelta
from difflib import SequenceMatcher
from pathlib import Path

from .pitchapi import PitchApiAuthError, PitchApiClient, PitchApiError
from .util import fold, read_json, to_int, whole

logger = logging.getLogger(__name__)

LEAGUE_NAME = "Premier League"
LEAGUE_COUNTRY = "ENG"
REFETCH_DAYS = 3  # matches this recent are fetched again, so a late correction is picked up


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def name_tokens(name):
    """Words of a name, folded; apostrophes are dropped, not split on (M'Baye -> mbaye)."""
    return re.sub(r"[^a-z ]+", " ", re.sub(r"['’`]", "", fold(name))).split()


def pick_spelling(variants):
    """One spelling out of the variants of the same name: the accented one if there is one, else the commonest."""
    counts = Counter(v for v in variants if v)
    if not counts:
        return None
    accented = [v for v in counts if re.search(r"[^\x00-\x7f]", v)]
    pool = accented or list(counts)
    return max(pool, key=lambda v: (counts[v], v))


def pick_league(leagues_body, name=LEAGUE_NAME, country=LEAGUE_COUNTRY):
    """The league entry for ``name`` in ``country``; None when there is no such league."""
    leagues = ((leagues_body or {}).get("data") or {}).get("leagues") or []
    for league in leagues:
        if (str(league.get("name") or "").strip().lower() == name.lower()
                and str(league.get("country_code") or "").upper() == country):
            return league
    return None


def _num(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _minute(event):
    return (_num(event.get("minute")) or 0) + (_num(event.get("minute_added")) or 0)


def dedupe_events(events):
    """Drop an event that is logged a second time within a minute (same type, player and period).

    Goals must also carry the same running score, so two real goals can never be merged.
    """
    kept = []
    for event in sorted(events or [], key=lambda e: (_minute(e), e.get("event_type") or "")):
        if event.get("event_type") in ("goal", "yellowcard", "redcard"):
            player = (event.get("player") or {}).get("id")
            if any(
                prev.get("event_type") == event.get("event_type")
                and (prev.get("player") or {}).get("id") == player
                and prev.get("period") == event.get("period")
                and abs(_minute(prev) - _minute(event)) <= 1
                and (event.get("event_type") != "goal"
                     or (prev.get("score_home"), prev.get("score_away")) == (event.get("score_home"), event.get("score_away")))
                for prev in kept[-8:]
            ):
                continue
        kept.append(event)
    return kept


# --------------------------------------------------------------------------- #
# Sources
# --------------------------------------------------------------------------- #
def _teams_from(matches):
    seen, teams = set(), []
    for match in matches:
        for side in ("home_team", "away_team"):
            team = match.get(side) or {}
            if team.get("id") and team["id"] not in seen:
                seen.add(team["id"])
                teams.append({"id": team["id"], "name": team.get("name"), "image_url": team.get("image_url")})
    return teams


class PitchApiSource:
    """Live PitchAPI. Never calls anything twice for the same match unless it is recent."""

    BUNDLE = (("lineups", "/lineups"), ("stats", "/stats"), ("players", "/players"), ("events", "/events"))

    def __init__(self, client=None, league_id=None):
        self.client = client or PitchApiClient()
        self._league = league_id  # a remembered id saves the /v1/leagues request
        self._cache = {}

    @property
    def known_league_id(self):
        return self._league

    def use_league(self, league_id):
        self._league = league_id or self._league

    def is_configured(self):
        return self.client.is_configured()

    def league_id(self):
        if self._league is None:
            league = pick_league(self.client.get("/v1/leagues"))
            if not league:
                raise PitchApiError(f"no {LEAGUE_NAME} with country_code {LEAGUE_COUNTRY} in /v1/leagues")
            self._league = league["id"]
        return self._league

    def _matches(self, status):
        if status not in self._cache:
            body = self.client.get(f"/v1/leagues/{self.league_id()}/matches", {"status": status})
            self._cache[status] = (body.get("data") or {}).get("matches") or []
        return self._cache[status]

    def finished_matches(self):
        return [m for m in self._matches("played") if m.get("status") == "finished"]

    def all_teams(self, minimum=0):
        """Clubs seen in the finished matches; the fixture list is asked for only if some club has not played yet."""
        teams = _teams_from(self._matches("played"))
        if len(teams) < minimum:
            teams = _teams_from(self._matches("played") + self._matches("upcoming"))
        return teams

    def bundle(self, match_id):
        """{'lineups': ..., 'stats': ..., 'players': ..., 'events': ...} (each the response's data), None for any that is missing."""
        out = {}
        for name, suffix in self.BUNDLE:
            try:
                out[name] = self.client.get(f"/v1/matches/{match_id}{suffix}").get("data")
            except PitchApiAuthError:
                raise
            except PitchApiError as exc:
                if exc.status == 404:
                    out[name] = None
                else:
                    raise
        return out


class HarvestSource:
    """Reads the folder written by ``manage.py harvest_pitchapi`` (the same four endpoints, from disk)."""

    def __init__(self, folder):
        self.folder = Path(folder)

    def is_configured(self):
        return (self.folder / "matches").is_dir()

    def _read(self, path):
        try:
            body = read_json(path)
        except (OSError, ValueError):
            return None
        return None if isinstance(body, dict) and "_error" in body else body

    def _matches(self, kind):
        found = []
        for path in sorted((self.folder / "matches").glob(f"*.{kind}.json")):
            found += ((self._read(path) or {}).get("data") or {}).get("matches") or []
        return found

    def finished_matches(self):
        return [m for m in self._matches("played") if m.get("status") == "finished"]

    known_league_id = None

    def use_league(self, league_id):
        pass

    def all_teams(self, minimum=0):
        return _teams_from(self._matches("played") + self._matches("upcoming"))

    def bundle(self, match_id):
        folder = self.folder / "match" / match_id
        out = {}
        for name in ("lineups", "stats", "players", "events"):
            body = self._read(folder / f"{name}.json")
            out[name] = (body or {}).get("data") if body else None
        return out


# --------------------------------------------------------------------------- #
# One match -> what the build stores
# --------------------------------------------------------------------------- #
def _starters_in_order(side):
    """Starting XI as GK, then defence -> attack, each line in the order the formation page expects.

    ``pitch_x`` is the line (0.1 is the goalkeeper), ``pitch_y`` the width. Lines are found by gaps in x rather
    than by rounding, because a back five can arrive as 0.36 and 0.37 on what is one line.
    """
    players = sorted(side.get("starters") or [], key=lambda s: _num(s.get("pitch_x")) or 0)
    line, last, keyed = -1, None, []
    for p in players:
        x = _num(p.get("pitch_x")) or 0
        if last is None or x - last > 0.05:
            line += 1
        last = x
        keyed.append((line, -(_num(p.get("pitch_y")) or 0), p))
    return [p for _line, _y, p in sorted(keyed, key=lambda k: (k[0], k[1]))]


def _stat_value(stats_body, key, side):
    """The 'All periods' value of a team statistic as text ('64', '282 (77%)'), None when absent."""
    for period in (stats_body or {}).get("periods") or []:
        if period.get("period") != "All":
            continue
        for group in period.get("groups") or []:
            for item in group.get("items") or []:
                if item.get("key") == key:
                    return item.get(side)
    return None


def team_stat_pair(stats_body, key):
    home, away = _stat_value(stats_body, key, "home"), _stat_value(stats_body, key, "away")
    to_int = lambda v: int(float(str(v).split(" ")[0])) if v not in (None, "") else None
    try:
        return to_int(home), to_int(away)
    except ValueError:
        return None, None


def adapt_match(match, lineups, stats):
    """A PitchAPI match as the flat record ``pl_data.derive_from_events`` reads (field names kept from the format the history already stores)."""
    possession = [_stat_value(stats, "BallPossesion", "home"), _stat_value(stats, "BallPossesion", "away")]
    event = {
        "match_id": match["id"],
        "match_date": match.get("date"),
        "match_time": (match.get("time_utc") or "")[11:16] or None,
        "match_status": "Finished",
        "match_hometeam_id": (match.get("home_team") or {}).get("id"),
        "match_awayteam_id": (match.get("away_team") or {}).get("id"),
        "match_hometeam_name": (match.get("home_team") or {}).get("name"),
        "match_awayteam_name": (match.get("away_team") or {}).get("name"),
        "match_hometeam_score": str(match.get("score_home")),
        "match_awayteam_score": str(match.get("score_away")),
        "match_hometeam_system": ((lineups or {}).get("home") or {}).get("formation") or "",
        "match_awayteam_system": ((lineups or {}).get("away") or {}).get("formation") or "",
        "lineup": {
            side: {"starting_lineups": [
                {"lineup_player": s.get("name"), "lineup_number": s.get("shirt_number"),
                 "lineup_position": str(n), "player_key": s.get("player_id")}
                for n, s in enumerate(_starters_in_order((lineups or {}).get(side) or {}), 1)]}
            for side in ("home", "away")
        },
        "statistics": [],
    }
    if all(v not in (None, "") for v in possession):
        event["statistics"] = [{"type": "Ball Possession", "home": f"{possession[0]}%", "away": f"{possession[1]}%"}]
    for side in ("home", "away"):
        coach = (((lineups or {}).get(side) or {}).get("coach") or {}).get("name")
        if coach:
            event[f"match_{side}team_coach"] = coach
    return event


# (short key stored, PitchAPI stat key, 'value' or 'total'). Only non-zero numbers are stored.
_FIELDS = [
    ("min", "minutes_played", "value"), ("g", "goals", "value"), ("a", "assists", "value"),
    ("og", "owngoal", "value"), ("sh", "total_shots", "value"), ("sv", "saves", "value"),
    ("gc", "goals_conceded", "value"), ("sib", "saves_inside_box", "value"), ("fl", "fouls", "value"),
    ("tk", "matchstats.headers.tackles", "value"), ("bl", "shot_blocks", "value"),
    ("cr", "accurate_crosses", "value"), ("crt", "accurate_crosses", "total"),
    ("in", "interceptions", "value"), ("cl", "clearances", "value"), ("dp", "dispossessed", "value"),
    ("dw", "duel_won", "value"), ("dl", "duel_lost", "value"),
    ("dbs", "dribbles_succeeded", "value"), ("dba", "dribbles_succeeded", "total"),
    ("ps", "accurate_passes", "value"), ("pst", "accurate_passes", "total"),
    ("kp", "chances_created", "value"), ("ww", "shots_woodwork", "value"),
    ("pw", "penalties_won", "value"), ("pc", "conceded_penalties", "value"), ("pm", "missed_penalty", "value"),
    ("xg", "expected_goals", "value"), ("xa", "expected_assists", "value"),
]


def _flat(player):
    """{stat key: stat dict}. A key can appear in several groups with the same numbers: the first one wins."""
    flat = {}
    for group in player.get("stats") or []:
        for item in (group.get("stats") or {}).values():
            key = item.get("key")
            if key and isinstance(item.get("stat"), dict):
                flat.setdefault(key, item["stat"])
    return flat


def player_lines(players_body, lineups, events):
    """{player id: {'t': team id, short numbers...}} for one match, with cards and penalty goals from the events."""
    shirts, captains = {}, set()
    for side in ("home", "away"):
        for group in ("starters", "subs"):
            for p in ((lineups or {}).get(side) or {}).get(group) or []:
                if p.get("player_id"):
                    shirts[p["player_id"]] = p.get("shirt_number")
                    if p.get("is_captain") and group == "starters":
                        captains.add(p["player_id"])
    lines, info = {}, {}
    for entry in players_body or []:
        player = entry.get("player") or {}
        pid = player.get("id")
        if not pid:
            continue
        flat = _flat(entry)
        line = {"t": entry.get("team_id")}
        for short, key, kind in _FIELDS:
            value = _num((flat.get(key) or {}).get(kind))
            if value:
                line[short] = round(value, 2) if isinstance(value, float) else value
        rating = _num((flat.get("rating_title") or {}).get("value"))
        if rating:
            line["rt"] = rating
        if shirts.get(pid) not in (None, ""):
            line["no"] = shirts[pid]
        if pid in captains:
            line["c"] = 1
        lines[pid] = line
        info[pid] = {"name": player.get("name"), "image": player.get("image_url"), "team": entry.get("team_id")}
    for event in dedupe_events(events):
        kind = event.get("event_type")
        player = event.get("player") or {}
        pid = player.get("id")
        if not pid or kind not in ("goal", "yellowcard", "redcard"):
            continue
        if pid not in lines:  # carded without being listed (rare): keep the card
            lines[pid] = {"t": event.get("team_id")}
            info[pid] = {"name": player.get("name"), "image": player.get("image_url"), "team": event.get("team_id")}
        line = lines[pid]
        if kind == "yellowcard":
            line["yc"] = line.get("yc", 0) + 1
        elif kind == "redcard":
            line["rc"] = line.get("rc", 0) + 1
        elif event.get("is_penalty") and not event.get("is_own_goal"):
            line["pg"] = line.get("pg", 0) + 1
    return lines, info


def cards_reconcile(lines, stats_body):
    """True when the cards credited to players equal the match's team statistics (None when those are missing)."""
    yellow, red = team_stat_pair(stats_body, "yellow_cards"), team_stat_pair(stats_body, "red_cards")
    if None in yellow or None in red:
        return None
    return (sum(l.get("yc", 0) for l in lines.values()) == sum(yellow)
            and sum(l.get("rc", 0) for l in lines.values()) == sum(red))


def build_match_record(match, bundle):
    """What the history keeps for one finished match, or None when the essentials (lineups, stats) are missing."""
    lineups, stats = bundle.get("lineups"), bundle.get("stats")
    if not lineups or not stats:
        return None
    lines, info = player_lines(bundle.get("players"), lineups, (bundle.get("events") or {}).get("events"))
    return {
        "event": adapt_match(match, lineups, stats),
        "players": lines,
        "info": info,
        "cards_ok": cards_reconcile(lines, stats),
    }


# --------------------------------------------------------------------------- #
# Refreshing the stored matches
# --------------------------------------------------------------------------- #
def fetch_reason(match, stored, cutoff, force=False):
    """Why a finished match has to be read (again), or None when what is stored is still right.

    new        never read before
    forced     the caller asked for everything again
    recent     played in the last few days: lineups and cards get corrected
    corrected  the league list now shows a different score than the one stored
    incomplete what is stored has no lineup or no player numbers
    """
    if stored is None:
        return "new"
    if force:
        return "forced"
    if (match.get("date") or "") >= cutoff:
        return "recent"
    event = stored.get("event") or {}
    if (str(match.get("score_home")), str(match.get("score_away"))) != (
            event.get("match_hometeam_score"), event.get("match_awayteam_score")):
        return "corrected"
    if not stored.get("players") or not event.get("match_hometeam_system") or not event.get("match_awayteam_system"):
        return "incomplete"
    return None


def refresh_history(source, history, today=None, log=print, force=False):
    """Read only the finished matches whose stored copy is missing or may be out of date.

    Returns {"finished", "new", "refreshed", "unchanged", "failed": [match ids], "reasons": {reason: count}}.
    A match that can't be read never aborts the build; a rejected key does (PitchApiAuthError propagates).
    """
    today = today or date.today()
    cutoff = (today - timedelta(days=REFETCH_DAYS)).isoformat()
    result = {"finished": 0, "new": 0, "refreshed": 0, "unchanged": 0, "failed": [], "reasons": {}}
    for match in source.finished_matches():
        result["finished"] += 1
        stored = history.pitch_match(match["id"])
        reason = fetch_reason(match, stored, cutoff, force)
        if reason is None:
            result["unchanged"] += 1
            continue
        try:
            record = build_match_record(match, source.bundle(match["id"]))
        except PitchApiAuthError:
            raise
        except PitchApiError as exc:
            log(f"    {match['id']}: {exc}")
            record = None
        if record is None:
            result["failed"].append(match["id"])
            continue
        history.add_pitch_match(match["id"], record)
        result["refreshed" if stored is not None else "new"] += 1
        result["reasons"][reason] = result["reasons"].get(reason, 0) + 1
    return result


# --------------------------------------------------------------------------- #
# Season numbers per player, and attaching them to a squad
# --------------------------------------------------------------------------- #
def aggregate_players(records):
    """{player id: season totals} over stored match records."""
    totals = defaultdict(lambda: defaultdict(float))
    rated = defaultdict(list)
    for record in records:
        for pid, line in (record.get("players") or {}).items():
            t = totals[pid]
            t["team"] = line.get("t") or t.get("team")
            t["apps"] += 1 if line.get("min") else 0
            for short, _key, _kind in _FIELDS:
                t[short] += line.get(short, 0)
            for short in ("yc", "rc", "pg"):
                t[short] += line.get(short, 0)
            if line.get("rt") and line.get("min"):
                rated[pid].append(line["rt"])
            if line.get("no") is not None:
                t["no"] = line["no"]  # records come oldest first, so this ends as the latest shirt
            if line.get("c"):
                t["captain"] = 1
    out = {}
    for pid, t in totals.items():
        d = dict(t)
        d["rating"] = round(sum(rated[pid]) / len(rated[pid]), 2) if rated.get(pid) else None
        out[pid] = d
    return out


def player_record(total, info):
    """A season record in the shape the dashboard reads (see ``pl_data.player_numbers``)."""
    t = total or {}
    g = lambda k: whole(t.get(k, 0))
    passes = t.get("pst", 0)
    extra = {
        "minutes": g("min"), "shots": g("sh"), "goals_conceded": g("gc"), "saves": g("sv"),
        "inside_box_saves": g("sib"), "fouls": g("fl"), "tackles": g("tk"), "blocks": g("bl"),
        "crosses": g("crt"), "interceptions": g("in"), "clearances": g("cl"), "dispossessed": g("dp"),
        "duels": whole(t.get("dw", 0) + t.get("dl", 0)), "duels_won": g("dw"),
        "dribble_attempts": g("dba"), "dribbles": g("dbs"),
        "pen_won": g("pw"), "pen_committed": g("pc"), "pen_scored": g("pg"), "pen_missed": g("pm"),
        "passes": whole(passes), "key_passes": g("kp"), "woodwork": g("ww"),
        "xg": round(t.get("xg", 0), 2), "xa": round(t.get("xa", 0), 2),
    }
    if passes:
        extra["pass_accuracy"] = round(100 * t.get("ps", 0) / passes, 1)
    return {
        "source": "pitchapi",
        "pitchapi_id": (info or {}).get("id"),
        "matched": bool(total),
        "shirt_number": _num(to_int(t.get("no"))),
        "appearances": g("apps"),
        "goals": g("g"),
        "assists": g("a"),
        "own_goals": g("og"),
        "yellow_cards": g("yc"),
        "red_cards": g("rc"),
        "rating": t.get("rating"),
        "injured": False,  # PitchAPI has no injury list
        "image": (info or {}).get("image"),
        "full_name": None,
        "captain": bool(t.get("captain")),
        "extra": extra,
    }


def match_players(squad, pool):
    """Pair squad players with PitchAPI players of the same club: {squad index: player id}.

    ``pool`` is {player id: {'name': ...}}. Four passes, each only accepting a unique candidate:
    same full name; one name contained in the other; same surname and first initial; near-identical surname and
    the same first initial.
    """
    pairs, taken = {}, set()
    squad_tokens = [name_tokens(p.get("name")) for p in squad]
    pool_tokens = {pid: name_tokens(info.get("name")) for pid, info in pool.items()}

    def run(test):
        for i, tokens in enumerate(squad_tokens):
            if i in pairs or not tokens:
                continue
            found = [pid for pid, other in pool_tokens.items() if pid not in taken and other and test(tokens, other)]
            if len(found) == 1:
                pairs[i] = found[0]
                taken.add(found[0])

    run(lambda a, b: a == b)
    run(lambda a, b: len(set(a) & set(b)) >= 1 and (set(a) <= set(b) or set(b) <= set(a)) and max(len(t) for t in set(a) & set(b)) >= 4)
    run(lambda a, b: a[-1] == b[-1] and a[0][:1] == b[0][:1])
    # transliterations (Yarmoliuk / Yarmolyuk): near-identical surname and the same first initial
    run(lambda a, b: a[0][:1] == b[0][:1] and SequenceMatcher(None, a[-1], b[-1]).ratio() >= 0.85)
    return pairs


def attach_pitch_stats(squad, team_players, played):
    """Add ``player['pitchapi']`` to every squad entry. Returns the club's PitchAPI players that matched nobody.

    ``team_players`` is {player id: {'name', 'image', 'total': season totals}}; ``played`` is how many matches
    the club has stored (a player who never appeared gets an honest all-zero record, but only if the club has played).
    """
    pairs = match_players(squad, team_players)
    for i, player in enumerate(squad):
        pid = pairs.get(i)
        if pid:
            data = team_players[pid]
            player["pitchapi"] = player_record(data.get("total"), {"id": pid, "image": data.get("image")})
        elif played:
            player["pitchapi"] = player_record(None, None)
    used = set(pairs.values())
    return [info["name"] for pid, info in team_players.items()
            if pid not in used and (info.get("total") or {}).get("min")]


def teams_from_records(records):
    """Clubs named by stored matches (for when the API can't be asked): [{'id', 'name'}]."""
    matches = []
    for record in records:
        event = record.get("event") or {}
        matches.append({side + "_team": {"id": event.get(f"match_{side}team_id"), "name": event.get(f"match_{side}team_name")}
                        for side in ("home", "away")})
    return _teams_from(matches)


def latest_coaches(records):
    """{team id: coach name} from the newest lineup, with accent variants folded to one spelling."""
    seen = defaultdict(list)  # team -> [(date, name)]
    for record in records:
        event = record.get("event") or {}
        for side in ("home", "away"):
            name, team = event.get(f"match_{side}team_coach"), event.get(f"match_{side}team_id")
            if name and team:
                seen[team].append((event.get("match_date") or "", name))
    coaches = {}
    for team, items in seen.items():
        items.sort()
        newest = fold(items[-1][1])
        coaches[team] = pick_spelling([n for _d, n in items if fold(n) == newest])
    return coaches
