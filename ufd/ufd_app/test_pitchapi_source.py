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
from .pl_fixtures import (CHELSEA, FakeFootballData, FakeFootballDataIo, MUN, SimpleSource, TODAY, WOLVES, card, full_source,
                          goal, lineups, pitch_match as match, player_entry, stat, starters, stats)


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
        record = ps.player_record(total, {"id": "p_1", "image": "https://img/p_1"})
        self.assertEqual((record["appearances"], record["goals"], record["rating"], record["shirt_number"]), (2, 3, 7.0, 10))
        self.assertEqual(record["extra"]["pass_accuracy"], 80.0)       # (30+10) / (40+10)
        self.assertEqual((record["extra"]["duels"], record["extra"]["duels_won"], record["extra"]["pen_scored"]), (4, 3, 1))
        self.assertTrue(record["captain"] and record["matched"])
        self.assertFalse(record["injured"])
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

    def test_there_is_no_injury_information(self):
        squad = [{"name": "Ann One"}]
        ps.attach_pitch_stats(squad, {"p_1": {"name": "Ann One", "total": {"min": 90}}}, 1)
        self.assertFalse(squad[0]["pitchapi"]["injured"])


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
# The whole build
# --------------------------------------------------------------------------- #
class FakeClient:
    """Stands in for PitchApiClient: answers by path and records every request."""

    def __init__(self, source):
        self.source, self.calls = source, []

    def is_configured(self):
        return True

    def get(self, path, params=None):
        self.calls.append(path if not params else f"{path}?{params['status']}")
        if path == "/v1/leagues":
            return {"data": {"leagues": [{"id": "l_en", "name": "Premier League", "country_code": "ENG"}]}}
        if path == "/v1/leagues/l_en/matches":
            return {"data": {"matches": self.source.matches if params["status"] == "played" else []}}
        mid, name = path.split("/")[3], path.split("/")[4]
        body = self.source.bundles[mid][name]
        if name == "players":
            return {"data": body}
        return {"data": body}


class BuildTests(SimpleTestCase):
    def build(self, pa, history=None, io=None, force=False, today=TODAY):
        with mock.patch.object(pl_data, "MIN_TEAMS", 3):
            return pl_data.build_dataset(FakeFootballData(), pa=pa, io=io or FakeFootballDataIo(configured=False),
                                         history=history, log=lambda *_: None, today=today, force=force)

    def stage(self, data, name):
        return next(s for s in data["meta"]["report"]["stages"] if s["name"] == name)

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
        self.assertEqual(pl_data.player_numbers(winger)["goals"], 3)
        self.assertEqual(data["meta"]["report"]["unmatched_pitchapi"], [])

    def test_nothing_of_apifootball_is_left_in_the_output(self):
        data = self.build(full_source())
        text = json.dumps(data).lower()
        self.assertNotIn("apifootball", text)
        self.assertNotIn("raw", {k for k in data} - {"meta", "standings", "teams"})

    def test_venue_is_the_stadium_name_only_the_rest_comes_from_club_extras(self):
        venue = self.build(full_source())["teams"]["61"]["profile"]["venue"]
        self.assertEqual(venue, {"name": "Chelsea Ground", "city": None, "address": None, "capacity": None, "surface": None})

    def test_players_who_appeared_but_match_nobody_are_reported(self):
        data = self.build(full_source())
        self.assertIn("Chelsea FC: Mystery Man", data["meta"]["report"]["unmatched_pitchapi_players"])

    def test_a_second_run_reads_no_match_again(self):
        history = History(path=None)
        first = self.build(full_source(), history=history)
        self.assertEqual(self.stage(first, "pitchapi matches (lineups, formations, possession, player numbers)")["detail"].split(":")[1].strip()[:6], "3 new,")
        again = full_source()
        data = self.build(again, history=history)
        self.assertEqual(again.fetched, [])                                          # all three are older than 3 days
        self.assertEqual(data["meta"]["report"]["updates"]["pitchapi_matches"],
                         {"finished": 3, "new": 0, "refreshed": 0, "unchanged": 3})
        self.assertEqual(self.stage(data, "updated this run")["detail"].split(";")[-1].strip(), "PitchAPI 0 new / 0 re-read / 3 untouched")

    def test_a_match_finishing_between_runs_is_the_only_one_read(self):
        history = History(path=None)
        self.build(full_source(), history=history)
        newer = full_source([match("m_4", MUN, WOLVES, "2026-10-04", (2, 2))])
        self.build(newer, history=history)
        self.assertEqual(newer.fetched, ["m_4"])

    def test_a_changed_score_in_the_league_list_reads_that_match_again(self):
        history = History(path=None)
        self.build(full_source(), history=history)
        corrected = full_source()
        corrected.matches[0] = dict(corrected.matches[0], score_home=3)             # was 2-0
        data = self.build(corrected, history=history)
        self.assertEqual(corrected.fetched, ["m_1"])
        self.assertEqual(self.stage(data, "pitchapi matches (lineups, formations, possession, player numbers)")["detail"].count("1 corrected"), 1)

    def test_a_stored_match_without_lineups_is_read_again(self):
        history = History(path=None)
        self.build(full_source(), history=history)
        history.pitch_match("m_2")["players"] = {}
        again = full_source()
        self.build(again, history=history)
        self.assertEqual(again.fetched, ["m_2"])

    def test_force_reads_everything_again(self):
        history = History(path=None)
        self.build(full_source(), history=history)
        again = full_source()
        self.build(again, history=history, force=True)
        self.assertEqual(sorted(again.fetched), ["m_1", "m_2", "m_3"])

    def test_the_league_id_is_remembered_so_a_quiet_run_makes_one_request(self):
        history = History(path=None)
        source = full_source()
        client = FakeClient(source)
        first_calls = lambda: list(client.calls)
        self.build(ps.PitchApiSource(client), history=history)
        # every club has played, so the fixture list is not asked for
        self.assertEqual(client.calls[:3], ["/v1/leagues", "/v1/leagues/l_en/matches?played", "/v1/matches/m_1/lineups"])
        self.assertEqual(history.meta["pitchapi_league_id"], "l_en")
        client.calls.clear()
        self.build(ps.PitchApiSource(client), history=history)
        self.assertEqual(client.calls, ["/v1/leagues/l_en/matches?played"])         # nothing new: no leagues, no fixtures, no match

    def test_the_fixture_list_is_only_asked_for_when_a_club_has_not_played_yet(self):
        source = full_source()
        client = FakeClient(source)
        pa = ps.PitchApiSource(client, league_id="l_en")
        self.assertEqual(len(pa.all_teams(minimum=3)), 3)
        self.assertNotIn("/v1/leagues/l_en/matches?upcoming", client.calls)
        self.assertEqual(len(pa.all_teams(minimum=4)), 3)                            # a 4th club would need the fixtures
        self.assertIn("/v1/leagues/l_en/matches?upcoming", client.calls)

    def test_without_a_key_the_stored_matches_are_still_used(self):
        history = History(path=None)
        self.build(full_source(), history=history)
        data = self.build(SimpleSource([], {}, configured=False), history=history)
        self.assertEqual(data["teams"]["61"]["stats"]["possession_source"], "pitchapi")
        self.assertIn("stored earlier", self.stage(data, "pitchapi")["detail"])

    def test_without_a_key_and_nothing_stored_formations_are_empty(self):
        data = self.build(None, history=History(path=None))
        chelsea = data["teams"]["61"]
        self.assertIsNone(chelsea["formation"]["default"])
        self.assertIsNone(chelsea["stats"]["possession_source"])
        self.assertIn("will be empty", self.stage(data, "pitchapi")["detail"])
        self.assertNotIn("pitchapi", chelsea["profile"]["squad"][0])

    def test_a_pitchapi_outage_falls_back_to_what_was_stored(self):
        history = History(path=None)
        self.build(full_source(), history=history)

        class Down(SimpleSource):
            def all_teams(self, minimum=0):
                raise PitchApiError("down", status=503)

        data = self.build(Down([], {}), history=history)
        chelsea = data["teams"]["61"]
        self.assertEqual(chelsea["stats"]["possession_source"], "pitchapi")
        self.assertEqual(chelsea["sources"]["pitchapi"], True)                       # clubs are re-found from the stored matches
        self.assertIn("stored earlier", self.stage(data, "pitchapi")["detail"])

    def test_a_pitchapi_outage_with_nothing_stored_still_builds(self):
        class Down(SimpleSource):
            def all_teams(self, minimum=0):
                raise PitchApiError("down", status=503)

        data = self.build(Down([], {}), history=History(path=None))
        self.assertIsNone(data["teams"]["61"]["stats"]["possession_source"])
        self.assertEqual(len(data["teams"]), 3)

    def test_coach_falls_back_to_the_lineup_name_when_football_data_has_none(self):
        from .pl_fixtures import FD_TEAMS
        teams = [dict(t, coach=None) for t in FD_TEAMS]
        with mock.patch.object(pl_data, "MIN_TEAMS", 3):
            data = pl_data.build_dataset(FakeFootballData(teams=teams), pa=full_source(), io=FakeFootballDataIo(configured=False),
                                         history=History(path=None), log=lambda *_: None, today=TODAY)
        self.assertEqual(data["teams"]["61"]["profile"]["coach"]["name"], "Chelsea Boss")


class FootballDataIoUpdateTests(SimpleTestCase):
    """footballdata.io is asked again only for a club that has played since, or after a day."""

    def build(self, io, history, standings=None, now=None):
        overrides = {"/competitions/PL/standings": standings} if standings else None
        with mock.patch.object(pl_data, "MIN_TEAMS", 3):
            return pl_data.build_dataset(FakeFootballData(overrides=overrides), pa=None, io=io, history=history,
                                         log=lambda *_: None, today=TODAY, now=now)

    def fake_io(self):
        from .pl_fixtures import io_stats
        return FakeFootballDataIo(stats={901: io_stats(901), 902: io_stats(902), 903: io_stats(903)})

    def test_second_run_makes_no_footballdata_io_request_at_all(self):
        history, io = History(path=None), self.fake_io()
        self.build(io, history)
        self.assertEqual((len(io.stat_calls), io.directory_calls), (3, 1))
        io.stat_calls.clear()
        data = self.build(io, history)
        self.assertEqual((io.stat_calls, io.directory_calls), ([], 1))               # club list and every club's stats reused
        self.assertEqual(data["meta"]["report"]["updates"]["footballdata_io"], {"fetched": 0, "reused": 3})

    def test_a_club_that_played_is_fetched_again_and_the_others_are_not(self):
        history, io = History(path=None), self.fake_io()
        self.build(io, history)
        io.stat_calls.clear()
        from .pl_fixtures import FD_STANDINGS
        import copy
        standings = copy.deepcopy(FD_STANDINGS)
        for table in standings["standings"]:
            for row in table["table"]:
                if row["team"]["id"] == 61:
                    row["playedGames"] += 1
        data = self.build(io, history, standings=standings)
        self.assertEqual([c[0] for c in io.stat_calls], [901])                       # only Chelsea (football-data id 61) played
        self.assertEqual(data["meta"]["report"]["updates"]["footballdata_io"], {"fetched": 1, "reused": 2})

    def test_stats_older_than_the_limit_are_fetched_again_even_without_a_match(self):
        from datetime import datetime, timedelta, timezone
        history, io = History(path=None), self.fake_io()
        start = datetime(2026, 10, 1, tzinfo=timezone.utc)
        self.build(io, history, now=start)
        io.stat_calls.clear()
        data = self.build(io, history, now=start + timedelta(hours=30))
        self.assertEqual(data["meta"]["report"]["updates"]["footballdata_io"], {"fetched": 3, "reused": 0})

    def test_the_club_list_is_looked_up_again_after_a_week(self):
        from datetime import datetime, timedelta, timezone
        history, io = History(path=None), self.fake_io()
        start = datetime(2026, 10, 1, tzinfo=timezone.utc)
        self.build(io, history, now=start)
        self.build(io, history, now=start + timedelta(days=3))
        self.assertEqual(io.directory_calls, 1)                                      # still the remembered list
        self.build(io, history, now=start + timedelta(days=8))
        self.assertEqual(io.directory_calls, 2)


class ConsumerTests(SimpleTestCase):
    def test_player_numbers_are_the_pitchapi_record_or_empty(self):
        self.assertEqual(pl_data.player_numbers({"pitchapi": {"goals": 3}}), {"goals": 3})
        self.assertEqual(pl_data.player_numbers({"pitchapi": None}), {})
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
        self.assertNotIn("apifootball", output.lower())

    def test_a_missing_harvest_folder_is_an_error_not_a_silent_fallback(self):
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError) as ctx:
            self.run_command("--pitchapi-harvest", "/no/such/folder")
        self.assertIn("ufd/pitchapi_raw", str(ctx.exception))

    def test_force_is_accepted(self):
        data, output = self.run_command("--pitchapi-harvest", "HARVEST", "--force")
        self.assertEqual(data["teams"]["61"]["sources"]["pitchapi"], True)

    def test_skip_pitchapi_leaves_formations_empty_without_stored_matches(self):
        data, output = self.run_command("--pitchapi-harvest", "HARVEST", "--skip-pitchapi")
        self.assertNotEqual(data["teams"]["61"]["stats"]["possession_source"], "pitchapi")
        self.assertEqual(data["teams"]["61"]["formation"]["default"], None)
        self.assertNotIn("from PitchAPI", output.replace("0 from PitchAPI", ""))
