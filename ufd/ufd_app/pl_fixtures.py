"""Small, hand-checkable API responses shaped like football-data.org and apifootball.com (for tests)."""
from . import pl_data

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


AP_LEAGUES = [{"apifootball_id": "152", "name": "Premier League"}]


def ap_team(key, name, dob_prefix, capacity):
    return {
        "team_key": key, "team_name": name, "team_country": "England", "team_founded": "1905",
        "team_badge": f"https://ap.example/{key}.png",
        "venue": {"venue_name": f"{name} Stadium", "venue_address": "1 Road", "venue_city": "Town",
                  "venue_capacity": capacity, "venue_surface": "grass"},
        "coaches": [{"coach_name": f"{name} Boss", "coach_country": "England", "coach_age": "50"},
                    {"coach_name": f"{name} Assistant", "coach_country": "Wales", "coach_age": "45"}],
        "players": [
            # matched to the keeper by birth date even though the name differs
            {"player_key": f"{key}1", "player_name": "G. Keeper", "player_birthdate": f"{dob_prefix}-01-01",
             "player_number": "1", "player_type": "Goalkeepers", "player_match_played": "3", "player_goals": "0",
             "player_assists": "0", "player_yellow_cards": "1", "player_red_cards": "0", "player_rating": "6.9",
             "player_injured": "No"},
            # matched by exact name (birth date missing on this side)
            {"player_key": f"{key}2", "player_name": f"{name.split()[0]} Winger", "player_birthdate": "",
             "player_number": "11", "player_type": "Forwards", "player_match_played": "3", "player_goals": "4",
             "player_assists": "2", "player_yellow_cards": "0", "player_red_cards": "0", "player_rating": "7.6",
             "player_injured": "Yes", "player_complete_name": "Winger Complete", "player_is_captain": "Yes",
             "player_minutes": "270", "player_shots_total": "9", "player_passes": "100", "player_passes_accuracy": "85",
             "player_pen_scored": "2", "player_pen_missed": "0", "player_pen_won": "1", "player_pen_comm": "0", "player_tackles": "0", "player_interceptions": "0", "player_saves": "0"},
            {"player_key": f"{key}9", "player_name": "Somebody Else", "player_birthdate": "1990-09-09",
             "player_number": "99", "player_type": "Midfielders"},
        ],
    }


AP_TEAMS = [
    ap_team("100", "Chelsea", "2000", "40173"),
    ap_team("200", "Manchester United", "2001", "74310"),
    ap_team("300", "Wolves", "2002", "31750"),
]
# Wolves matches the football-data "Wolverhampton" player names, so give it matching winger/keeper names
AP_TEAMS[1]["players"][1]["player_name"] = "Man United Winger"
AP_TEAMS[2]["players"][1]["player_name"] = "Wolverhampton Winger"


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
AP_EVENTS = [
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


class FakeApiFootball:
    def __init__(self, configured=True, events=None, league_id="152"):
        self.configured = configured
        self.events = AP_EVENTS if events is None else events
        self.league_id = league_id
        self.event_windows = []

    def is_configured(self):
        return self.configured

    def premier_league_id(self):
        return self.league_id

    def get_teams(self, league_id):
        return [dict(t) for t in AP_TEAMS]

    def get_standings(self, league_id):
        return [{"team_id": "100", "overall_league_position": "1"}]

    def get_events(self, league_id, date_from, date_to):
        self.event_windows.append((date_from, date_to))
        return [e for e in self.events if date_from <= e["match_date"] <= date_to] or None


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

    def is_configured(self):
        return self.configured

    def premier_league_id(self):
        return self.league_id

    def get_current_teams(self, league_id):
        return IO_SEASON_ID, [dict(t) for t in IO_TEAMS if t["season"]["season_id"] == IO_SEASON_ID]

    def get_team_stats(self, team_id, league_id, season_id):
        self.stat_calls.append((team_id, league_id, season_id))
        if self.error:
            raise self.error
        return self.stats.get(team_id)
