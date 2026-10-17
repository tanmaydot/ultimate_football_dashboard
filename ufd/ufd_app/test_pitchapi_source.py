"""Tests for pitchapi_source (formations, possession and player numbers from PitchAPI). No network, no key."""
import json
import tempfile
from datetime import date
from unittest import mock
from pathlib import Path

from django.test import SimpleTestCase

from . import dashboard, pitchapi_source as ps, pl_data
from .history import History
from .pitchapi import PitchApiAuthError, PitchApiError
from .pl_fixtures import FakeApiFootball, FakeFootballData, FakeFootballDataIo, TODAY


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


def match(mid, home, away, day, score=(1, 0), status="finished"):
    return {"id": mid, "date": day, "time_utc": f"{day}T14:00:00Z", "status": status,
            "home_team": {"id": home[0], "name": home[1]}, "away_team": {"id": away[0], "name": away[1]},
            "score_home": score[0], "score_away": score[1]}


class SimpleSource:
    """A source with a fixed match list; bundles come from a dict."""

    def __init__(self, matches, bundles, teams=None, configured=True):
        self.matches, self.bundles, self.configured = matches, bundles, configured
        self.fetched = []
        self._teams = teams

    def is_configured(self):
        return self.configured

    def finished_matches(self):
        return self.matches

    def all_teams(self):
        if self._teams is not None:
            return self._teams
        return ps._teams_from(self.matches)

    def bundle(self, match_id):
        self.fetched.append(match_id)
        result = self.bundles[match_id]
        if isinstance(result, Exception):
            raise result
        return result


# --------------------------------------------------------------------------- #
class HelperTests(SimpleTestCase):
    def test_league_is_picked_by_country_not_by_name(self):
        body = {"data": {"leagues": [
            {"id": "l_ru", "name": "Premier League", "country_code": "RUS"},
            {"id": "l_en", "name": "Premier League", "country_code": "ENG"},
            {"id": "l_eg", "name": "Premier League", "country_code": "EGY"}]}}
        self.assertEqual(ps.pick_league(body)["id"], "l_en")
        self.assertIsNone(ps.pick_league({"data": {"leagues": [{"id": "x", "name": "Premier League", "country_code": "RUS"}]}}))

    def test_the_harvester_also_prefers_the_english_league(self):
        from .pitchapi_harvest import find_league
        body = {"data": {"leagues": [{"id": "l_ru", "name": "Premier League", "country_code": "RUS"},
                                     {"id": "l_en", "name": "Premier League", "country_code": "ENG"}]}}
        self.assertEqual(find_league(body)["id"], "l_en")

    def test_accented_spelling_wins(self):
        self.assertEqual(ps.pick_spelling(["Sergej Jakirovic", "Sergej Jakirović", "Sergej Jakirovic"]), "Sergej Jakirović")
        self.assertEqual(ps.pick_spelling(["A B", "A B", "A C"]), "A B")
        self.assertIsNone(ps.pick_spelling([None, ""]))

    def test_a_goal_logged_twice_a_minute_apart_counts_once(self):
        events = [goal(84, "t1", "p1", (0, 1)), goal(85, "t1", "p1", (0, 1)), goal(90, "t2", "p2", (1, 1))]
        kept = ps.dedupe_events(events)
        self.assertEqual([e["minute"] for e in kept], [84, 90])

    def test_two_real_goals_in_a_row_are_both_kept(self):
        events = [goal(70, "t1", "p1", (1, 0)), goal(71, "t1", "p1", (2, 0))]  # the running score differs
        self.assertEqual(len(ps.dedupe_events(events)), 2)

    def test_cards_are_deduped_but_substitutions_are_not(self):
        events = [card("yellowcard", 94, "t1", "p1"), card("yellowcard", 95, "t1", "p1"),
                  {"event_type": "substitution", "minute": 60, "minute_added": 0, "period": "SecondHalf",
                   "team_id": "t1", "player": {"id": "p3"}},
                  {"event_type": "substitution", "minute": 61, "minute_added": 0, "period": "SecondHalf",
                   "team_id": "t1", "player": {"id": "p3"}}]
        kept = ps.dedupe_events(events)
        self.assertEqual([e["event_type"] for e in kept].count("yellowcard"), 1)
        self.assertEqual([e["event_type"] for e in kept].count("substitution"), 2)


class AdaptMatchTests(SimpleTestCase):
    def setUp(self):
        self.match = match("m_1", ("t_h", "Home FC"), ("t_a", "Away FC"), "2026-09-01", (2, 1))
        self.event = ps.adapt_match(self.match, lineups(), stats())

    def test_shape_is_what_derive_from_events_reads(self):
        e = self.event
        self.assertEqual((e["match_id"], e["match_date"], e["match_status"]), ("m_1", "2026-09-01", "Finished"))
        self.assertEqual((e["match_hometeam_system"], e["match_awayteam_system"]), ("4-3-3", "4-4-2"))
        self.assertEqual(e["statistics"], [{"type": "Ball Possession", "home": "60%", "away": "40%"}])  # 'All' period, not the half
        self.assertEqual(e["match_hometeam_coach"], "Home Boss")

    def test_xi_runs_goalkeeper_then_defence_to_attack_and_right_side_first_in_each_line(self):
        xi = [p["lineup_player"] for p in self.event["lineup"]["home"]["starting_lineups"]]
        self.assertEqual(len(xi), 11)
        self.assertEqual(xi[0], "H Keeper")
        # back four: y runs 0.2 (left) .. 0.8 (right); the page mirrors each row, so the right-back is listed first
        self.assertEqual(xi[1:5], ["H P4", "H P3", "H P2", "H P1"])
        positions = [p["lineup_position"] for p in self.event["lineup"]["home"]["starting_lineups"]]
        self.assertEqual(positions, [str(n) for n in range(1, 12)])

    def test_derive_and_build_formation_work_on_it_unchanged(self):
        derived = pl_data.derive_from_events([self.event])
        home = derived["t_h"]
        self.assertEqual(home["possession"], [60.0])
        summary = pl_data.formation_summary(home)
        self.assertEqual(summary["default"], "4-3-3")
        self.assertEqual(len(summary["starting_xi"]), 11)
        pitch = dashboard.build_formation("4-3-3", summary["starting_xi"], [])
        self.assertEqual([len(line["slots"]) for line in pitch["lines"]], [1, 4, 3, 3])

    def test_a_back_five_split_across_two_x_values_stays_one_line(self):
        side = lineups(hf="5-4-1")["home"]
        for s in side["starters"][1:6]:
            s["pitch_x"] = 0.37 if s["player_id"].endswith("3") else 0.36
        ordered = ps._starters_in_order(side)
        self.assertEqual(ordered[0]["player_id"], "p_H0")
        self.assertEqual({s["player_id"] for s in ordered[1:6]}, {f"p_H{i}" for i in range(1, 6)})  # all five before midfield
        ys = [s["pitch_y"] for s in ordered[1:6]]
        self.assertEqual(ys, sorted(ys, reverse=True))                                          # right side first

    def test_missing_possession_gives_no_statistics(self):
        e = ps.adapt_match(self.match, lineups(), {"periods": []})
        self.assertEqual(e["statistics"], [])


class PlayerLineTests(SimpleTestCase):
    def test_numbers_cards_penalties_and_shirts(self):
        body = [player_entry("p_1", "Ann", "t_h", goals=2, assists=1, passes=(30, 40)),
                player_entry("p_2", "Bob", "t_a", minutes=0, rating=0)]
        events = [goal(10, "t_h", "p_1", (1, 0), pen=True), goal(40, "t_h", "p_1", (2, 0)),
                  goal(50, "t_a", "p_9", (2, 1), own=True),
                  card("yellowcard", 30, "t_h", "p_1"), card("yellowcard", 31, "t_h", "p_1"),  # a duplicate
                  card("redcard", 80, "t_a", "p_2")]
        lines, info = ps.player_lines(body, {"home": {"starters": [{"player_id": "p_1", "shirt_number": "9", "is_captain": True}]}}, events)
        ann = lines["p_1"]
        self.assertEqual((ann["g"], ann["a"], ann["min"], ann["ps"], ann["pst"], ann["tk"]), (2, 1, 90, 30, 40, 2))
        self.assertEqual((ann["yc"], ann["pg"], ann["no"], ann["c"]), (1, 1, "9", 1))
        self.assertEqual(lines["p_2"]["rc"], 1)
        self.assertNotIn("min", lines["p_2"])                    # zero numbers are not stored
        self.assertNotIn("pg", lines["p_9"] if "p_9" in lines else {})  # an own goal is never a penalty goal
        self.assertEqual(info["p_1"], {"name": "Ann", "image": "https://img/p_1.webp", "team": "t_h"})

    def test_a_card_for_a_player_who_is_not_listed_is_kept(self):
        lines, info = ps.player_lines([], {}, [card("yellowcard", 20, "t_h", "p_x")])
        self.assertEqual(lines["p_x"]["yc"], 1)
        self.assertIn("p_x", info)

    def test_cards_reconcile_against_team_statistics(self):
        lines = {"a": {"yc": 1}, "b": {"yc": 1, "rc": 1}}
        self.assertTrue(ps.cards_reconcile(lines, stats(yellow=(1, 1), red=(0, 1))))
        self.assertFalse(ps.cards_reconcile(lines, stats(yellow=(1, 0), red=(0, 1))))
        self.assertIsNone(ps.cards_reconcile(lines, {"periods": []}))


class AggregationTests(SimpleTestCase):
    def records(self):
        def rec(day, players):
            return {"event": {"match_date": day}, "players": players}
        return [
            rec("2026-08-21", {"p_1": {"t": "t_h", "min": 90, "g": 1, "a": 0, "ps": 30, "pst": 40, "rt": 8.0, "no": "9", "dw": 3, "dl": 1}}),
            rec("2026-08-28", {"p_1": {"t": "t_h", "min": 30, "g": 2, "ps": 10, "pst": 10, "rt": 6.0, "no": "10", "pg": 1, "c": 1},
                               "p_2": {"t": "t_h"}}),   # listed but never played
            rec("2026-09-04", {"p_1": {"t": "t_h", "min": 0}}),
        ]

    def test_totals(self):
        totals = ps.aggregate_players(self.records())
        p = totals["p_1"]
        self.assertEqual((p["apps"], p["min"], p["g"], p["pg"], p["no"], p["captain"]), (2, 120, 3, 1, "10", 1))
        self.assertEqual(p["rating"], 7.0)
        self.assertIsNone(totals["p_2"]["rating"])
        self.assertEqual(totals["p_2"]["apps"], 0)

    def test_record_shape_for_the_dashboard(self):
        total = ps.aggregate_players(self.records())["p_1"]
        record = ps.player_record(total, {"id": "p_1", "image": "https://img/p_1"}, injured=True)
        self.assertEqual((record["appearances"], record["goals"], record["rating"], record["shirt_number"]), (2, 3, 7.0, 10))
        self.assertEqual(record["extra"]["pass_accuracy"], 80.0)       # (30+10) / (40+10)
        self.assertEqual((record["extra"]["duels"], record["extra"]["duels_won"], record["extra"]["pen_scored"]), (4, 3, 1))
        self.assertTrue(record["injured"] and record["captain"] and record["matched"])
        unused = ps.player_record(None, None)
        self.assertEqual((unused["appearances"], unused["goals"], unused["matched"], unused["rating"]), (0, 0, False, None))
        self.assertNotIn("pass_accuracy", unused["extra"])


class PlayerMatchingTests(SimpleTestCase):
    def pool(self, *names):
        return {f"p_{i}": {"name": n} for i, n in enumerate(names)}

    def test_exact_then_contained_then_initial_and_surname(self):
        squad = [{"name": "Erling Haaland"}, {"name": "Igor Thiago"}, {"name": "H. Kane"}, {"name": "Unknown Guy"}]
        pairs = ps.match_players(squad, self.pool("Erling Haaland", "Thiago", "Harry Kane", "Somebody Else"))
        self.assertEqual(pairs, {0: "p_0", 1: "p_1", 2: "p_2"})

    def test_accents_do_not_matter(self):
        pairs = ps.match_players([{"name": "Joško Gvardiol"}], self.pool("Josko Gvardiol"))
        self.assertEqual(pairs, {0: "p_0"})

    def test_apostrophes_stroked_letters_and_transliterations(self):
        squad = [{"name": "Ibrahim M'Baye"}, {"name": "Đorđe Petrović"}, {"name": "Yegor Yarmolyuk"}]
        pairs = ps.match_players(squad, self.pool("Ibrahim Mbaye", "Djordje Petrović", "Yehor Yarmoliuk"))
        self.assertEqual(pairs, {0: "p_0", 1: "p_1", 2: "p_2"})

    def test_similar_surnames_with_different_initials_are_not_paired(self):
        self.assertEqual(ps.match_players([{"name": "Tom Smyth"}], self.pool("Sam Smith")), {})

    def test_an_ambiguous_name_is_left_unmatched_rather_than_guessed(self):
        squad = [{"name": "Jack Smith"}]
        self.assertEqual(ps.match_players(squad, self.pool("John Smith", "Jake Smith")), {})

    def test_a_pitch_player_is_never_used_twice(self):
        squad = [{"name": "Ben Davies"}, {"name": "Ben Davies"}]
        self.assertEqual(len(ps.match_players(squad, self.pool("Ben Davies"))), 1)

    def test_unmatched_players_who_played_are_reported(self):
        squad = [{"name": "Ann One"}]
        team = {"p_1": {"name": "Ann One", "image": None, "total": {"min": 90, "g": 1}},
                "p_2": {"name": "Zed Nobody", "image": None, "total": {"min": 45}},
                "p_3": {"name": "Bench Warmer", "image": None, "total": {"min": 0}}}
        left = ps.attach_pitch_stats(squad, team, played=3)
        self.assertEqual(left, ["Zed Nobody"])
        self.assertEqual(squad[0]["pitchapi"]["goals"], 1)

    def test_a_squad_player_who_never_appeared_gets_an_honest_zero_only_once_the_club_has_played(self):
        squad = [{"name": "Reserve Keeper"}]
        ps.attach_pitch_stats(squad, {}, played=0)
        self.assertNotIn("pitchapi", squad[0])
        ps.attach_pitch_stats(squad, {}, played=2)
        self.assertEqual(squad[0]["pitchapi"]["appearances"], 0)

    def test_injured_flag_survives_from_apifootball(self):
        squad = [{"name": "Ann One", "apifootball": {"injured": True}}]
        ps.attach_pitch_stats(squad, {"p_1": {"name": "Ann One", "total": {"min": 90}}}, 1)
        self.assertTrue(squad[0]["pitchapi"]["injured"])


class CoachTests(SimpleTestCase):
    def test_newest_coach_with_one_spelling(self):
        def rec(day, home, away):
            return {"event": {"match_date": day, "match_hometeam_id": "t1", "match_awayteam_id": "t2",
                              "match_hometeam_coach": home, "match_awayteam_coach": away}}
        coaches = ps.latest_coaches([rec("2026-08-21", "Old Boss", "Sergej Jakirovic"),
                                     rec("2026-08-28", "New Boss", "Sergej Jakirović"),
                                     rec("2026-09-04", "New Boss", "Sergej Jakirovic")])
        self.assertEqual(coaches, {"t1": "New Boss", "t2": "Sergej Jakirović"})


class RefreshTests(SimpleTestCase):
    def setUp(self):
        h, a = ("t_h", "Home FC"), ("t_a", "Away FC")
        self.matches = [match("m_old", h, a, "2026-08-21"), match("m_new", a, h, "2026-10-04", (0, 2))]
        good = lambda: {"lineups": lineups("H", "A"), "stats": stats(), "players": [player_entry("p_H1", "H P1", "t_h", goals=1)],
                        "events": {"events": []}}
        self.bundles = {"m_old": good(), "m_new": good()}

    def test_first_run_fetches_everything_then_only_recent_matches(self):
        source, history = SimpleSource(self.matches, self.bundles), History(path=None)
        first = ps.refresh_history(source, history, today=date(2026, 10, 5), log=lambda *_: None)
        self.assertEqual((first["finished"], first["new"], first["refreshed"], first["failed"]), (2, 2, 0, []))
        source.fetched.clear()
        second = ps.refresh_history(source, history, today=date(2026, 10, 5), log=lambda *_: None)
        self.assertEqual(source.fetched, ["m_new"])      # m_old is older than 3 days: not asked for again
        self.assertEqual((second["new"], second["refreshed"]), (0, 1))
        self.assertEqual(len(history.pitch_matches), 2)

    def test_a_match_without_lineups_is_reported_not_stored(self):
        self.bundles["m_new"]["lineups"] = None
        history = History(path=None)
        result = ps.refresh_history(SimpleSource(self.matches, self.bundles), history, today=date(2026, 10, 5), log=lambda *_: None)
        self.assertEqual(result["failed"], ["m_new"])
        self.assertEqual(list(history.pitch_matches), ["m_old"])

    def test_one_failing_request_does_not_stop_the_rest(self):
        self.bundles["m_old"] = PitchApiError("boom", status=500)
        history = History(path=None)
        result = ps.refresh_history(SimpleSource(self.matches, self.bundles), history, today=date(2026, 10, 5), log=lambda *_: None)
        self.assertEqual((result["new"], result["failed"]), (1, ["m_old"]))

    def test_a_rejected_key_stops_everything(self):
        self.bundles["m_old"] = PitchApiAuthError("nope", status=401)
        with self.assertRaises(PitchApiAuthError):
            ps.refresh_history(SimpleSource(self.matches, self.bundles), History(path=None), today=date(2026, 10, 5), log=lambda *_: None)

    def test_history_keeps_player_details_once_and_survives_a_save_and_load(self):
        history = History(path=None)
        ps.refresh_history(SimpleSource(self.matches, self.bundles), history, today=date(2026, 10, 5), log=lambda *_: None)
        self.assertEqual(history.data["pitch_players"]["p_H1"]["name"], "H P1")
        self.assertNotIn("info", next(iter(history.pitch_matches.values())))
        with tempfile.TemporaryDirectory() as folder:
            history.path = Path(folder) / "h.json"
            history.save()
            again = History.load(history.path)
        self.assertEqual(len(again.pitch_matches), 2)
        self.assertEqual([r["event"]["match_date"] for r in again.pitch_records()], ["2026-08-21", "2026-10-04"])

    def test_an_old_history_file_without_pitch_sections_still_loads(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "h.json"
            path.write_text(json.dumps({"version": 1, "season_start": "2026-08-21", "matches": {}, "results": {},
                                        "team_stats": {}, "player_stats": {}, "io_stats": {}}))
            history = History.load(path)
        self.assertEqual(history.pitch_matches, {})
        self.assertEqual(history.summary()["pitch_matches_stored"], 0)


class HarvestSourceTests(SimpleTestCase):
    def test_reads_a_harvest_folder_and_skips_error_stubs(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "matches").mkdir()
            played = [match("m_1", ("t_h", "Home"), ("t_a", "Away"), "2026-08-21"),
                      match("m_2", ("t_h", "Home"), ("t_a", "Away"), "2026-08-28", status="postponed")]
            upcoming = [match("m_3", ("t_b", "Other"), ("t_h", "Home"), "2026-11-01", status="not_started")]
            (root / "matches" / "current.played.json").write_text(json.dumps({"data": {"matches": played}}))
            (root / "matches" / "current.upcoming.json").write_text(json.dumps({"data": {"matches": upcoming}}))
            (root / "match" / "m_1").mkdir(parents=True)
            (root / "match" / "m_1" / "lineups.json").write_text(json.dumps({"data": lineups()}))
            (root / "match" / "m_1" / "stats.json").write_text(json.dumps({"_error": {"status": 404}, "_path": "x"}))
            source = ps.HarvestSource(root)
            self.assertTrue(source.is_configured())
            self.assertEqual([m["id"] for m in source.finished_matches()], ["m_1"])
            self.assertEqual({t["id"] for t in source.all_teams()}, {"t_h", "t_a", "t_b"})
            bundle = source.bundle("m_1")
        self.assertEqual(bundle["lineups"]["home"]["formation"], "4-3-3")
        self.assertIsNone(bundle["stats"])
        self.assertIsNone(bundle["players"])
        self.assertFalse(ps.HarvestSource("/no/such/folder").is_configured())


# --------------------------------------------------------------------------- #
# The whole build, with PitchAPI in place of apifootball for matches and players
# --------------------------------------------------------------------------- #
CHELSEA, MUN, WOLVES = ("t_che", "Chelsea"), ("t_mun", "Manchester United"), ("t_wol", "Wolverhampton Wanderers")


def full_source(extra_matches=()):
    matches = [match("m_1", CHELSEA, MUN, "2026-08-22", (2, 0)), match("m_2", WOLVES, CHELSEA, "2026-09-12", (1, 1)),
               match("m_3", CHELSEA, WOLVES, "2026-09-26", (3, 0)), *extra_matches]
    bundles = {}
    for m in matches:
        h, a = m["home_team"]["id"], m["away_team"]["id"]
        players = [player_entry(f"p_{t}{role}", f"Chelsea {role_name}" if t == "t_che" else f"{t} {role_name}", t, goals=goals)
                   for t in (h, a) for role, role_name, goals in (("k", "Keeper", 0), ("w", "Winger", 1))]
        if "t_che" in (h, a):
            players.append(player_entry("p_mystery", "Mystery Man", "t_che", minutes=30))
        bundles[m["id"]] = {"lineups": lineups(h, a, "4-2-3-1" if h == "t_che" else "4-3-3", "4-3-3" if a == "t_che" else "5-4-1",
                                               coaches=("Chelsea Boss" if h == "t_che" else "Other Boss",
                                                        "Chelsea Boss" if a == "t_che" else "Other Boss")),
                            "stats": stats(possession=(60, 40) if h == "t_che" else (45, 55)),
                            "players": players, "events": {"events": []}}
    return SimpleSource(matches, bundles, teams=ps._teams_from(matches))


class FullBuildTests(SimpleTestCase):
    def build(self, pa, ap=None, history=None):
        with mock.patch.object(pl_data, "MIN_TEAMS", 3):
            return self._build(pa, ap, history)

    def _build(self, pa, ap, history):
        return pl_data.build_dataset(FakeFootballData(), ap if ap is not None else FakeApiFootball(), log=lambda *_: None,
                                     today=TODAY, io=FakeFootballDataIo(configured=False), history=history, pa=pa)

    def test_formation_possession_and_players_come_from_pitchapi(self):
        data = self.build(full_source())
        chelsea = data["teams"]["61"]
        self.assertEqual(chelsea["sources"]["pitchapi"], True)
        self.assertEqual(chelsea["sources"]["pitchapi_team_id"], "t_che")
        self.assertEqual(chelsea["formation"]["default"], "4-2-3-1")                  # played twice at home
        self.assertEqual(chelsea["formation"]["usage"], {"4-2-3-1": 2, "4-3-3": 1})
        self.assertEqual(chelsea["stats"]["possession_source"], "pitchapi")
        self.assertEqual(chelsea["stats"]["possession"], round((60 + 55 + 60) / 3, 1))
        winger = next(p for p in chelsea["profile"]["squad"] if p["name"] == "Chelsea Winger")
        self.assertEqual((winger["pitchapi"]["goals"], winger["pitchapi"]["appearances"]), (3, 3))
        self.assertTrue(winger["pitchapi"]["injured"])        # apifootball's flag is kept: PitchAPI has none
        self.assertEqual(pl_data.player_numbers(winger)["goals"], 3)                  # not apifootball's 4
        self.assertEqual(data["meta"]["report"]["unmatched_pitchapi"], [])

    def test_apifootball_is_still_used_for_the_venue_but_not_for_matches(self):
        ap = FakeApiFootball()
        data = self.build(full_source(), ap=ap)
        self.assertEqual(ap.event_windows, [])                                       # no per-month event calls
        self.assertEqual(data["teams"]["61"]["profile"]["venue"]["capacity"], 40173)
        stages = {s["name"]: s for s in data["meta"]["report"]["stages"]}
        self.assertIn("skipped", stages["match events (formations, possession)"]["detail"])

    def test_players_who_appeared_but_match_nobody_are_reported(self):
        data = self.build(full_source())
        self.assertIn("Chelsea FC: Mystery Man", data["meta"]["report"]["unmatched_pitchapi_players"])

    def test_the_build_stores_matches_so_a_second_run_asks_for_nothing_old(self):
        history = History(path=None)
        self.build(full_source(), history=history)
        self.assertEqual(len(history.pitch_matches), 3)
        again = full_source()
        self.build(again, history=history)
        self.assertEqual(again.fetched, [])                                          # all three are older than 3 days

    def test_without_pitchapi_the_old_apifootball_path_is_unchanged(self):
        data = self.build(None)
        chelsea = data["teams"]["61"]
        self.assertEqual(chelsea["stats"]["possession_source"], "apifootball")
        self.assertEqual(chelsea["formation"]["default"], "4-2-3-1")
        self.assertNotIn("pitchapi", chelsea["profile"]["squad"][0])
        stages = {s["name"]: s for s in data["meta"]["report"]["stages"]}
        self.assertFalse(stages["pitchapi"]["ok"])

    def test_a_pitchapi_outage_falls_back_to_what_was_stored(self):
        history = History(path=None)
        self.build(full_source(), history=history)

        class Down(SimpleSource):
            def all_teams(self):
                raise PitchApiError("down", status=503)

        data = self.build(Down([], {}), history=history)
        chelsea = data["teams"]["61"]
        self.assertEqual(chelsea["stats"]["possession_source"], "pitchapi")
        self.assertEqual(chelsea["sources"]["pitchapi"], True)                       # clubs are re-found from the stored matches
        stages = [s for s in data["meta"]["report"]["stages"] if s["name"] == "pitchapi"]
        self.assertIn("stored earlier", stages[0]["detail"])

    def test_a_pitchapi_outage_with_nothing_stored_falls_back_to_apifootball(self):
        class Down(SimpleSource):
            def all_teams(self):
                raise PitchApiError("down", status=503)

        data = self.build(Down([], {}), history=History(path=None))
        self.assertEqual(data["teams"]["61"]["stats"]["possession_source"], "apifootball")

    def test_coach_falls_back_to_the_lineup_name_when_football_data_has_none(self):
        teams = [dict(t) for t in __import__("ufd_app.pl_fixtures", fromlist=["FD_TEAMS"]).FD_TEAMS]
        for t in teams:
            t["coach"] = None
        with mock.patch.object(pl_data, "MIN_TEAMS", 3):
            data = pl_data.build_dataset(FakeFootballData(teams=teams), None, log=lambda *_: None, today=TODAY,
                                         io=FakeFootballDataIo(configured=False), history=History(path=None), pa=full_source())
        self.assertEqual(data["teams"]["61"]["profile"]["coach"]["name"], "Chelsea Boss")


class ConsumerTests(SimpleTestCase):
    def test_player_numbers_prefer_pitchapi_and_fall_back(self):
        self.assertEqual(pl_data.player_numbers({"pitchapi": {"goals": 3}, "apifootball": {"goals": 9}}), {"goals": 3})
        self.assertEqual(pl_data.player_numbers({"pitchapi": None, "apifootball": {"goals": 9}}), {"goals": 9})
        self.assertEqual(pl_data.player_numbers({}), {})
        self.assertEqual(pl_data.player_numbers(None), {})

    def test_squad_widget_uses_pitchapi_numbers(self):
        squad = [{"id": 1, "name": "Ann One", "position": "Centre-Forward", "pitchapi": ps.player_record(
            {"min": 90, "g": 4, "a": 2, "no": "9"}, {"id": "p"})}]
        groups = dashboard.group_squad(squad)
        entry = groups[0][1][0]
        self.assertEqual((entry["goals"], entry["assists"], entry["shirt"]), (4, 2, 9))


class CommandTests(SimpleTestCase):
    """build_pl_data --pitchapi-harvest, end to end through the management command."""

    def write_harvest(self, root, source):
        (root / "matches").mkdir()
        (root / "matches" / "current.played.json").write_text(json.dumps({"data": {"matches": source.matches}}))
        for mid, bundle in source.bundles.items():
            folder = root / "match" / mid
            folder.mkdir(parents=True)
            for name, body in bundle.items():
                (folder / f"{name}.json").write_text(json.dumps({"data": body}))

    def run_command(self, *args):
        from io import StringIO
        from django.core.management import call_command
        out = StringIO()
        with tempfile.TemporaryDirectory() as folder, \
             mock.patch.object(pl_data, "MIN_TEAMS", 3), \
             mock.patch("ufd_app.pl_data.FootballDataClient", return_value=FakeFootballData()), \
             mock.patch("ufd_app.pl_data.ApiFootballSource", return_value=FakeApiFootball()), \
             mock.patch("ufd_app.pl_data.FootballDataIoSource", return_value=FakeFootballDataIo()):
            root = Path(folder)
            harvest = root / "harvest"
            harvest.mkdir()
            self.write_harvest(harvest, full_source())
            target = root / "teams.json"
            call_command("build_pl_data", "--output", str(target), "--no-history",
                         *[a.replace("HARVEST", str(harvest)) for a in args], stdout=out)
            return json.loads(target.read_text(encoding="utf-8")), out.getvalue()

    def test_harvest_option_builds_from_the_folder(self):
        data, output = self.run_command("--pitchapi-harvest", "HARVEST")
        self.assertEqual(data["teams"]["61"]["stats"]["possession_source"], "pitchapi")
        self.assertIn("3 from PitchAPI", output)

    def test_a_missing_harvest_folder_is_an_error_not_a_silent_fallback(self):
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError) as ctx:
            self.run_command("--pitchapi-harvest", "/no/such/folder")
        self.assertIn("ufd/pitchapi_raw", str(ctx.exception))

    def test_skip_pitchapi_keeps_the_old_path(self):
        data, output = self.run_command("--pitchapi-harvest", "HARVEST", "--skip-pitchapi")
        self.assertEqual(data["teams"]["61"]["stats"]["possession_source"], "apifootball")
        self.assertNotIn("from PitchAPI", output)
