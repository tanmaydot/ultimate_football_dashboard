"""Small, hand-checkable API responses shaped like football-data.org, PitchAPI and footballdata.io (for tests)."""
from . import pitchapi_source, pl_data

SEASON = {"startDate": "2026-08-15", "endDate": "2027-05-24", "currentMatchday": 7}
TODAY = pl_data.date(2026, 10, 5)

COMPETITION = {
    "name": "Premier League",
    "currentSeason": SEASON,
    "seasons": [
        {"startDate": "2025-08-15", "winner": {"id": 64}},
        {"startDate": "2024-08-16", "winner": {"id": 65}},
        {"startDate": "2023-08-11", "winner": {"id": 61}},
    ],
}


def fd_team(team_id, name, short, tla, dob_prefix, coach=None):
    return {
        "id": team_id, "name": name, "shortName": short, "tla": tla,
        "crest": f"https://crests.example/{team_id}.png", "founded": 1900 + team_id % 100,
        "clubColors": "Blue / White", "venue": f"{short} Ground", "website": f"https://{tla.lower()}.example",
        "address": f"1 {short} Road", "area": {"name": "England"},
        "runningCompetitions": [{"name": "Premier League"}],
        "coach": coach or {"name": f"{short} Boss", "nationality": "England", "dateOfBirth": "1975-01-01",
                           "contract": {"start": "2026-01", "until": "2028-06"}},
        "squad": [
            {"id": team_id * 10 + 1, "name": f"{short} Keeper", "position": "Goalkeeper", "nationality": "Spain",
             "dateOfBirth": f"{dob_prefix}-01-01"},
            {"id": team_id * 10 + 2, "name": f"{short} Winger", "position": "Left Winger", "nationality": "Brazil",
             "dateOfBirth": f"{dob_prefix}-02-02"},
            {"id": team_id * 10 + 3, "name": f"{short} Unknown", "position": "Centre-Back", "nationality": "France",
             "dateOfBirth": f"{dob_prefix}-03-03"},
        ],
    }


FD_TEAMS = [
    fd_team(61, "Chelsea FC", "Chelsea", "CHE", "2000"),
    fd_team(66, "Manchester United FC", "Man United", "MUN", "2001"),
    fd_team(76, "Wolverhampton Wanderers FC", "Wolverhampton", "WOL", "2002"),
]


def row(position, team, played, won, draw, lost, gf, ga, points):
    return {"position": position, "team": {k: team[k] for k in ("id", "name", "shortName", "tla", "crest")},
            "playedGames": played, "won": won, "draw": draw, "lost": lost, "goalsFor": gf, "goalsAgainst": ga,
            "goalDifference": gf - ga, "points": points, "form": "W,D,W"}


FD_STANDINGS = {"standings": [
    {"type": "HOME", "table": []},
    {"type": "TOTAL", "table": [
        row(1, FD_TEAMS[0], 3, 2, 1, 0, 6, 1, 7),
        row(2, FD_TEAMS[2], 3, 0, 1, 2, 1, 4, 1),
        row(3, FD_TEAMS[1], 3, 0, 0, 3, 0, 2, 0),
    ]},
]}


_match_ids = iter(range(1000, 2000))


def match(home, away, home_goals, away_goals):
    return {"id": next(_match_ids), "utcDate": "2026-09-01T14:00:00Z", "homeTeam": {"id": home}, "awayTeam": {"id": away},
            "score": {"fullTime": {"home": home_goals, "away": away_goals}}}


# Chelsea: 2 clean sheets (2-0 v MUN, 3-0 v WOL) and a 1-1 draw. MUN and WOL: none.
FD_MATCHES = {"matches": [match(61, 66, 2, 0), match(76, 61, 1, 1), match(61, 76, 3, 0),
                          match(66, 76, None, None)]}


def lineup(prefix, shuffle=True):
    players = [{"lineup_player": f"{prefix} Player {i}", "lineup_number": str(i), "lineup_position": str(i),
                "player_key": f"{prefix}{i}"} for i in range(1, 12)]
    if shuffle:
        players = players[5:] + players[:5]  # prove we sort by position, not list order
    return {"starting_lineups": players, "substitutes": [], "coach": [], "missing_players": []}


def event(match_id, day, home_key, away_key, home_system, away_system, home_poss, away_poss, status="Finished"):
    return {
        "match_id": match_id, "match_date": day, "match_status": status,
        "match_hometeam_id": home_key, "match_awayteam_id": away_key,
        "match_hometeam_system": home_system, "match_awayteam_system": away_system,
        "lineup": {"home": lineup(f"H{match_id}-"), "away": lineup(f"A{match_id}-")},
        "statistics": [{"type": "Shots Total", "home": "10", "away": "4"},
                       {"type": "Ball Possession", "home": f"{home_poss}%", "away": f"{away_poss}%"}],
    }


# Chelsea (key 100): 4-2-3-1 twice, 4-3-3 once -> default 4-2-3-1; possession (60 + 55 + 52) / 3 = 55.7
MATCH_RECORDS = [
    event("m1", "2026-08-20", "100", "200", "4-2-3-1", "4-3-3", 60, 40),
    event("m2", "2026-09-10", "300", "100", "3-4-3", "4-2-3-1", 45, 55),
    event("m3", "2026-09-20", "100", "300", "4-3-3", "3-4-3", 52, 48),
    event("m4", "2026-10-03", "100", "200", "4-4-2", "4-4-2", 50, 50, status="Postponed"),
    event("m1", "2026-08-20", "100", "200", "4-2-3-1", "4-3-3", 60, 40),  # duplicate, must count once
]


class FakeFootballData:
    """Stands in for FootballDataClient: answers from a path -> payload dict and records the calls."""

    def __init__(self, overrides=None, teams=None):
        self.calls = []
        self.payloads = {
            "/competitions/PL": COMPETITION,
            "/competitions/PL/teams": {"teams": teams if teams is not None else [dict(t) for t in FD_TEAMS]},
            "/competitions/PL/standings": FD_STANDINGS,
            "/competitions/PL/matches": FD_MATCHES,
        }
        self.payloads.update(overrides or {})

    def is_configured(self):
        return True

    def get(self, path, params=None):
        self.calls.append((path, params))
        payload = self.payloads[path]
        if isinstance(payload, Exception):
            raise payload
        return payload


# --------------------------------------------------------------------------- #
# footballdata.io, shaped like the real responses captured by probe_footballdata_io
# --------------------------------------------------------------------------- #
IO_LEAGUE_ID = 15
IO_SEASON_ID = 103535
IO_OLD_SEASON_ID = 189


def io_team(team_id, name_clean, season_id=IO_SEASON_ID, year=20262027):
    return {
        "team_id": team_id, "team_name": name_clean + " FC", "team_name_clean": name_clean,
        "team_logo": f"https://footballdata.io/img/team/{team_id}.png", "founded": "1886",
        "season": {"season_id": season_id, "year": year},
        "stadium": {"name": None, "address": None},
    }


def io_stats(team_id, possession=58, matches=5, league_id=IO_LEAGUE_ID, season_id=IO_SEASON_ID):
    return {
        "team": {"team_id": team_id},
        "league": {"league_id": league_id},
        "season": {"season_id": season_id},
        "summary": {"matches_played": matches, "points_per_game": 0},
        "clean_sheets": {"total": 3},
        "possession": {"average": possession, "home_average": 0, "away_average": 58},
        "xg": {"xg_for": 7.5, "xg_against": -0.32},
        "shots": {"recorded_matches": 5, "shots_per_match": 14.2, "shots_on_target_per_match": 5.1},
        "corners": {"recorded_matches": 0, "for_per_match": 0},
        "fouls": {"committed_per_match": 10.5},
        "cards": {"for_per_match": 1.2},
    }


# Real responses list every team once per season, with HTML-escaped names.
IO_TEAMS = [
    io_team(901, "Chelsea"),
    io_team(901, "Chelsea", IO_OLD_SEASON_ID, 20252026),
    io_team(902, "Manchester United"),
    io_team(903, "Wolverhampton Wanderers"),
    io_team(903, "Wolverhampton Wanderers", IO_OLD_SEASON_ID, 20252026),
]


class FakeFootballDataIo:
    def __init__(self, configured=True, league_id=IO_LEAGUE_ID, stats=None, error=None):
        self.configured = configured
        self.league_id = league_id
        self.stats = {901: io_stats(901), 902: io_stats(902, possession=0), 903: None} if stats is None else stats
        self.error = error
        self.stat_calls = []
        self.directory_calls = 0  # how often the league / club list was looked up

    def is_configured(self):
        return self.configured

    def premier_league_id(self):
        self.directory_calls += 1
        return self.league_id

    def get_current_teams(self, league_id):
        return IO_SEASON_ID, [dict(t) for t in IO_TEAMS if t["season"]["season_id"] == IO_SEASON_ID]

    def get_team_stats(self, team_id, league_id, season_id):
        self.stat_calls.append((team_id, league_id, season_id))
        if self.error:
            raise self.error
        return self.stats.get(team_id)


# --------------------------------------------------------------------------- #
# Synthetic PitchAPI bodies (same shapes as the real harvest)
# --------------------------------------------------------------------------- #
def starters(team, formation, shirt_start=1):
    """GK on x=0.1, then one x level per line; within a line y runs left (low) to right (high), as in the real data."""
    out = [{"player_id": f"p_{team}0", "name": f"{team} Keeper", "shirt_number": str(shirt_start), "position_id": 11,
            "is_captain": False, "pitch_x": 0.1, "pitch_y": 0.5}]
    index = 1
    for level, count in enumerate(int(n) for n in formation.split("-")):
        for k in range(count):
            out.append({"player_id": f"p_{team}{index}", "name": f"{team} P{index}", "shirt_number": str(shirt_start + index),
                        "position_id": 30 + level, "is_captain": index == 1, "pitch_x": round(0.1 + 0.25 * (level + 1), 2),
                        "pitch_y": round((k + 1) / (count + 1), 2)})
            index += 1
    return out


def lineups(home="H", away="A", hf="4-3-3", af="4-4-2", coaches=("Home Boss", "Away Boss")):
    return {"home": {"formation": hf, "confirmed": True, "coach": {"name": coaches[0]}, "starters": starters(home, hf),
                     "subs": [{"player_id": f"p_{home}s", "name": f"{home} Sub", "shirt_number": "30", "is_captain": False}]},
            "away": {"formation": af, "confirmed": True, "coach": {"name": coaches[1]}, "starters": starters(away, af, 21),
                     "subs": []}}


def stats(possession=(60, 40), yellow=(1, 0), red=(0, 0)):
    def item(key, h, a):
        return {"key": key, "home": str(h), "away": str(a)}
    return {"periods": [
        {"period": "All", "groups": [{"group_name": "Top", "items": [
            item("BallPossesion", *possession), item("yellow_cards", *yellow), item("red_cards", *red)]}]},
        {"period": "FirstHalf", "groups": [{"items": [item("BallPossesion", 99, 1)]}]},
    ]}


def stat(key, value, total=None, kind="integer"):
    body = {"type": kind, "value": value}
    if total is not None:
        body["total"] = total
    return {"key": key, "stat": body}


def player_entry(pid, name, team, minutes=90, goals=0, assists=0, rating=7.0, passes=(40, 50), tackles=2, shots=1):
    return {"player": {"id": pid, "name": name, "image_url": f"https://img/{pid}.webp"}, "team_id": team, "stats": [
        {"key": "top_stats", "stats": {
            "Goals": stat("goals", goals), "Assists": stat("assists", assists), "Minutes played": stat("minutes_played", minutes),
            "Rating": stat("rating_title", rating, kind="double"), "Accurate passes": stat("accurate_passes", passes[0], passes[1]),
            "Total shots": stat("total_shots", shots)}},
        {"key": "defense", "stats": {"Tackles": stat("matchstats.headers.tackles", tackles)}},
    ]}


def goal(minute, team, pid, score=(0, 0), own=False, pen=False, added=0):
    return {"event_type": "goal", "minute": minute, "minute_added": added, "period": "FirstHalf", "team_id": team,
            "player": {"id": pid, "name": pid}, "is_own_goal": own, "is_penalty": pen,
            "score_home": score[0], "score_away": score[1]}


def card(kind, minute, team, pid, added=0):
    return {"event_type": kind, "minute": minute, "minute_added": added, "period": "SecondHalf", "team_id": team,
            "player": {"id": pid, "name": pid}, "is_own_goal": False, "is_penalty": False}


def pitch_match(mid, home, away, day, score=(1, 0), status="finished"):
    return {"id": mid, "date": day, "time_utc": f"{day}T14:00:00Z", "status": status,
            "home_team": {"id": home[0], "name": home[1]}, "away_team": {"id": away[0], "name": away[1]},
            "score_home": score[0], "score_away": score[1]}


class SimpleSource:
    """A source with a fixed match list; bundles come from a dict."""

    known_league_id = None

    def __init__(self, matches, bundles, teams=None, configured=True):
        self.matches, self.bundles, self.configured = matches, bundles, configured
        self.fetched = []
        self._teams = teams

    def use_league(self, league_id):
        pass

    def is_configured(self):
        return self.configured

    def finished_matches(self):
        return self.matches

    def all_teams(self, minimum=0):
        if self._teams is not None:
            return self._teams
        return pitchapi_source._teams_from(self.matches)

    def bundle(self, match_id):
        self.fetched.append(match_id)
        result = self.bundles[match_id]
        if isinstance(result, Exception):
            raise result
        return result


# --------------------------------------------------------------------------- #


CHELSEA, MUN, WOLVES = ("t_che", "Chelsea"), ("t_mun", "Manchester United"), ("t_wol", "Wolverhampton Wanderers")


def full_source(extra_matches=()):
    matches = [pitch_match("m_1", CHELSEA, MUN, "2026-08-22", (2, 0)), pitch_match("m_2", WOLVES, CHELSEA, "2026-09-12", (1, 1)),
               pitch_match("m_3", CHELSEA, WOLVES, "2026-09-26", (3, 0)), *extra_matches]
    bundles = {}
    for m in matches:
        h, a = m["home_team"]["id"], m["away_team"]["id"]
        players = [player_entry(f"p_{t}{role}", f"Chelsea {role_name}" if t == "t_che" else f"{t} {role_name}", t, goals=goals,
                                assists=1 if role == "w" else 0)
                   for t in (h, a) for role, role_name, goals in (("k", "Keeper", 0), ("w", "Winger", 1))]
        if "t_che" in (h, a):
            players.append(player_entry("p_mystery", "Mystery Man", "t_che", minutes=30))
        bundles[m["id"]] = {"lineups": lineups(h, a, "4-2-3-1" if h == "t_che" else "4-3-3", "4-3-3" if a == "t_che" else "5-4-1",
                                               coaches=("Chelsea Boss" if h == "t_che" else "Other Boss",
                                                        "Chelsea Boss" if a == "t_che" else "Other Boss")),
                            "stats": stats(possession=(60, 40) if h == "t_che" else (45, 55)),
                            "players": players,
                            # in the first match the Chelsea winger scored from the spot
                            "events": {"events": [goal(10, "t_che", "p_t_chew", (1, 0), pen=True)] if m["id"] == "m_1" else []}}
    return SimpleSource(matches, bundles, teams=pitchapi_source._teams_from(matches))
