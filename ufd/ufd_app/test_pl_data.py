import json
import tempfile
from collections import Counter
from datetime import date
from io import StringIO
from pathlib import Path
from unittest import mock

import requests
from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, override_settings

from . import pl_data
from .footballdata_io import FootballDataIoError
from .history import History
from .pl_fixtures import FD_TEAMS, MATCH_RECORDS, TODAY, FakeFootballData, FakeFootballDataIo, SimpleSource, full_source


def build(fd=None, pa=None, **kwargs):
    with mock.patch.object(pl_data, "MIN_TEAMS", 3):
        return pl_data.build_dataset(fd or FakeFootballData(), pa=pa, log=lambda *_: None, today=TODAY, **kwargs)


class TeamMatchingTests(SimpleTestCase):
    def test_normalise_handles_suffixes_symbols_and_nicknames(self):
        n = pl_data.normalise_team_name
        self.assertEqual(n("Brighton & Hove Albion FC"), n("Brighton"))
        self.assertEqual(n("AFC Bournemouth"), n("Bournemouth"))
        self.assertEqual(n("Wolverhampton Wanderers FC"), n("Wolves"))
        self.assertEqual(n("Tottenham Hotspur FC"), n("Spurs"))
        self.assertEqual(n("Nottingham Forest FC"), n("Nott'm Forest"))
        self.assertEqual(n("Manchester United FC"), n("Man Utd"))
        self.assertNotEqual(n("Manchester United FC"), n("Manchester City FC"))

    def test_all_prem_style_names_pair_up(self):
        fd = [{"id": i, "name": a, "shortName": a} for i, (a, _) in enumerate([
            ("Manchester City FC", "Man City"), ("Newcastle United FC", "Newcastle"),
            ("West Ham United FC", "West Ham"), ("Leeds United FC", "Leeds"),
            ("Sunderland AFC", "Sunderland")])]
        ap = [{"team_name": b} for _, b in [("", "Man City"), ("", "Newcastle"), ("", "West Ham"),
                                            ("", "Leeds"), ("", "Sunderland")]]
        matches, un_fd, un_ap = pl_data.match_teams(fd, ap)
        self.assertEqual((len(matches), un_fd, un_ap), (5, [], []))

    def test_fuzzy_match_is_used_only_when_clearly_best(self):
        fd = [{"id": 1, "name": "Sheffield Wednesday FC"}]
        ap = [{"team_name": "Sheffield Wednesdy"}, {"team_name": "Arsenal"}]
        matches, un_fd, un_ap = pl_data.match_teams(fd, ap)
        self.assertEqual(matches[1][1], "fuzzy")
        self.assertEqual(un_ap, ["Arsenal"])

    def test_unrelated_teams_are_reported_not_guessed(self):
        matches, un_fd, un_ap = pl_data.match_teams([{"id": 1, "name": "Chelsea FC"}], [{"team_name": "Arsenal"}])
        self.assertEqual(matches, {})
        self.assertEqual(un_fd, ["Chelsea FC"])
        self.assertEqual(un_ap, ["Arsenal"])

    def test_one_candidate_team_is_never_used_twice(self):
        fd = [{"id": 1, "name": "Chelsea FC"}, {"id": 2, "name": "Chelsea"}]
        matches, un_fd, _ = pl_data.match_teams(fd, [{"team_name": "Chelsea"}])
        self.assertEqual(len(matches), 1)
        self.assertEqual(len(un_fd), 1)


class DerivedNumbersTests(SimpleTestCase):
    def test_clean_sheets_skip_unplayed_matches(self):
        from .pl_fixtures import FD_MATCHES
        self.assertEqual(pl_data.count_clean_sheets(FD_MATCHES["matches"]), {61: 2, 66: 0, 76: 0})

    def test_events_ignore_unfinished_and_duplicates(self):
        derived = pl_data.derive_from_events(MATCH_RECORDS)
        chelsea = derived["100"]
        self.assertEqual(chelsea["usage"], Counter({"4-2-3-1": 2, "4-3-3": 1}))
        self.assertEqual(chelsea["possession"], [60.0, 55.0, 52.0])

    def test_default_formation_is_most_used_and_xi_is_in_position_order(self):
        summary = pl_data.formation_summary(pl_data.derive_from_events(MATCH_RECORDS)["100"])
        self.assertEqual(summary["default"], "4-2-3-1")
        self.assertEqual(summary["matches_counted"], 3)
        self.assertEqual(summary["based_on_match_date"], "2026-09-10")  # latest 4-2-3-1 game
        self.assertEqual(len(summary["starting_xi"]), 11)
        self.assertTrue(summary["starting_xi"][0].endswith("Player 1"))
        self.assertTrue(summary["starting_xi"][10].endswith("Player 11"))

    def test_every_formation_is_listed_with_its_own_latest_xi(self):
        summary = pl_data.formation_summary(pl_data.derive_from_events(MATCH_RECORDS)["100"])
        self.assertEqual([(f["formation"], f["count"], f["last_date"]) for f in summary["formations"]],
                         [("4-2-3-1", 2, "2026-09-10"), ("4-3-3", 1, "2026-09-20")])
        self.assertTrue(all(len(f["starting_xi"]) == 11 for f in summary["formations"]))
        self.assertEqual(summary["last_used"], "4-3-3")
        self.assertEqual(summary["last_used_date"], "2026-09-20")

    def test_formation_tie_goes_to_the_most_recent(self):
        events = [
            {"match_id": "a", "match_date": "2026-08-20", "match_status": "Finished", "match_hometeam_id": "1",
             "match_awayteam_id": "2", "match_hometeam_system": "4-4-2", "match_awayteam_system": "4-3-3"},
            {"match_id": "b", "match_date": "2026-09-20", "match_status": "Finished", "match_hometeam_id": "1",
             "match_awayteam_id": "2", "match_hometeam_system": "3-5-2", "match_awayteam_system": "4-3-3"},
        ]
        summary = pl_data.formation_summary(pl_data.derive_from_events(events)["1"])
        self.assertEqual(summary["default"], "3-5-2")
        self.assertIsNone(summary["starting_xi"])  # no lineup data -> no names, just the shape

    def test_unusable_lineups_give_no_names(self):
        short = {"starting_lineups": [{"lineup_player": "A", "lineup_position": "1"}]}
        self.assertIsNone(pl_data._starting_xi(short))
        dupes = {"starting_lineups": [{"lineup_player": str(i), "lineup_position": "1"} for i in range(11)]}
        self.assertIsNone(pl_data._starting_xi(dupes))
        self.assertIsNone(pl_data._starting_xi(None))

    def test_no_events_means_no_formation(self):
        summary = pl_data.formation_summary({"usage": Counter(), "latest": {}})
        self.assertIsNone(summary["default"])

    def test_premier_league_titles(self):
        from .pl_fixtures import COMPETITION
        self.assertEqual(pl_data.premier_league_titles(COMPETITION, 61), {"count": 1, "since": 2023})
        self.assertEqual(pl_data.premier_league_titles(COMPETITION, 999)["count"], 0)
        self.assertIsNone(pl_data.premier_league_titles({"seasons": []}, 61))


class SquadTests(SimpleTestCase):
    def test_squad_comes_from_football_data(self):
        keeper, winger, unknown = pl_data.build_squad(FD_TEAMS[0]["squad"])
        self.assertEqual(keeper["date_of_birth"], "2000-01-01")
        self.assertEqual([p["name"] for p in (keeper, winger, unknown)], ["Chelsea Keeper", "Chelsea Winger", "Chelsea Unknown"])
        self.assertNotIn("pitchapi", keeper)         # season numbers are attached afterwards, from PitchAPI

    def test_missing_input_is_fine(self):
        self.assertEqual(pl_data.build_squad(None), [])
        self.assertEqual(pl_data.build_squad([{"id": 1, "name": "X"}])[0]["name"], "X")


class BuildDatasetTests(SimpleTestCase):
    def test_full_build_merges_the_sources(self):
        data = build(pa=full_source())
        self.assertEqual(set(data["teams"]), {"61", "66", "76"})
        chelsea = data["teams"]["61"]

        self.assertEqual(chelsea["stats"]["position"], 1)
        self.assertEqual(chelsea["stats"]["points"], 7)
        self.assertEqual(chelsea["stats"]["avg_points"], 2.33)
        self.assertEqual(chelsea["stats"]["clean_sheets"], 2)
        self.assertEqual(chelsea["stats"]["possession"], 58.3)
        self.assertEqual(chelsea["stats"]["possession_matches"], 3)

        self.assertEqual(chelsea["formation"]["default"], "4-2-3-1")
        self.assertEqual(chelsea["profile"]["venue"]["name"], "Chelsea Ground")  # football-data's; the rest is club_extras
        self.assertEqual(chelsea["profile"]["coach"]["name"], "Chelsea Boss")
        self.assertEqual(chelsea["profile"]["staff"], [])
        self.assertEqual(chelsea["profile"]["premier_league_titles"], {"count": 1, "since": 2023})
        self.assertEqual(chelsea["sources"], {"football_data": True, "footballdata_io": False, "footballdata_io_team_id": None,
                                              "pitchapi": True, "pitchapi_team_id": "t_che",
                                              "pitchapi_unmatched_players": ["Mystery Man"]})
        self.assertIn("squad", chelsea["raw"]["football_data"])
        self.assertEqual(set(chelsea["raw"]), {"football_data", "footballdata_io"})
        self.assertEqual([r["position"] for r in data["standings"]], [1, 2, 3])
        self.assertEqual(data["teams"]["76"]["sources"]["pitchapi_team_id"], "t_wol")  # "Wolverhampton Wanderers FC" pairs up
        self.assertEqual(data["meta"]["report"]["unmatched_pitchapi"], [])
        json.dumps(data)  # must be serialisable

    def test_works_without_pitchapi(self):
        data = build(pa=None)
        chelsea = data["teams"]["61"]
        self.assertFalse(chelsea["sources"]["pitchapi"])
        self.assertIsNone(chelsea["stats"]["possession"])
        self.assertIsNone(chelsea["formation"]["default"])
        self.assertEqual(chelsea["stats"]["clean_sheets"], 2)
        stages = {s["name"]: s for s in data["meta"]["report"]["stages"]}
        self.assertFalse(stages["pitchapi"]["ok"])
        self.assertTrue(stages["updated this run"]["ok"])

    def test_the_report_says_what_was_updated(self):
        history = History(path=None)
        data = build(pa=full_source(), history=history)
        stages = {s["name"]: s["detail"] for s in data["meta"]["report"]["stages"]}
        self.assertEqual(stages["updated this run"],
                         "4 new result(s); PitchAPI 3 new / 0 re-read / 0 untouched")
        again = build(pa=full_source(), history=history)
        stages = {s["name"]: s["detail"] for s in again["meta"]["report"]["stages"]}
        self.assertEqual(stages["updated this run"],
                         "0 new result(s); PitchAPI 0 new / 0 re-read / 3 untouched")

    def test_clean_sheet_failure_is_reported_not_fatal(self):
        fd = FakeFootballData(overrides={"/competitions/PL/matches": pl_data.FetchError("boom")})
        data = build(fd=fd)
        self.assertIsNone(data["teams"]["61"]["stats"]["clean_sheets"])
        self.assertFalse([s for s in data["meta"]["report"]["stages"] if "clean sheets" in s["name"]][0]["ok"])

    def test_too_few_teams_refuses_to_build(self):
        with self.assertRaises(pl_data.FetchError):
            pl_data.build_dataset(FakeFootballData(teams=[dict(FD_TEAMS[0])]), pa=None, log=lambda *_: None, today=TODAY)

    def test_teams_missing_squad_are_topped_up_from_the_team_endpoint(self):
        bare = [{"id": t["id"], "name": t["name"], "shortName": t["shortName"], "tla": t["tla"]} for t in FD_TEAMS]
        fd = FakeFootballData(teams=bare)
        for team in FD_TEAMS:
            fd.payloads[f"/teams/{team['id']}"] = team
        data = build(fd=fd)
        self.assertEqual(len([c for c in fd.calls if c[0].startswith("/teams/")]), 3)
        self.assertEqual(len(data["teams"]["61"]["profile"]["squad"]), 3)

    def test_a_football_data_failure_aborts(self):
        fd = FakeFootballData(overrides={"/competitions/PL/standings": pl_data.FetchError("down")})
        with self.assertRaises(pl_data.FetchError):
            build(fd=fd)

    def test_start_of_season_with_no_matches_is_handled(self):
        with mock.patch.object(pl_data, "MIN_TEAMS", 3):
            data = pl_data.build_dataset(FakeFootballData(), pa=SimpleSource([], {}), log=lambda *_: None, today=date(2026, 8, 1))
        self.assertIsNone(data["teams"]["61"]["formation"]["default"])
        self.assertEqual(len(data["teams"]), 3)


def response(status=200, payload=None, headers=None):
    r = mock.Mock()
    r.status_code = status
    r.headers = headers or {}
    r.json.return_value = payload if payload is not None else {}
    return r


class FootballDataClientTests(SimpleTestCase):
    def make_client(self, **kw):
        self.slept = []
        self.now = [0.0]

        def sleep(seconds):
            self.slept.append(seconds)
            self.now[0] += seconds

        return pl_data.FootballDataClient(api_key="secret-key", sleep=sleep, clock=lambda: self.now[0], **kw)

    @mock.patch("ufd_app.pl_data.requests.get")
    def test_calls_are_spaced_out(self, get):
        get.return_value = response(payload={"ok": True})
        client = self.make_client(min_interval=6.5)
        client.get("/a")
        client.get("/b")
        client.get("/c")
        self.assertEqual(self.slept, [6.5, 6.5])
        self.assertEqual(get.call_args_list[0].kwargs["headers"], {"X-Auth-Token": "secret-key"})

    @mock.patch("ufd_app.pl_data.requests.get")
    def test_rate_limit_waits_and_retries_once(self, get):
        get.side_effect = [response(429, headers={"Retry-After": "30"}), response(payload={"ok": 1})]
        client = self.make_client(min_interval=0)
        self.assertEqual(client.get("/a"), {"ok": 1})
        self.assertIn(30, self.slept)

    @mock.patch("ufd_app.pl_data.requests.get", return_value=response(429))
    def test_persistent_rate_limit_raises(self, _):
        with self.assertRaises(pl_data.FetchError):
            self.make_client(min_interval=0).get("/a")

    @mock.patch("ufd_app.pl_data.requests.get", return_value=response(403))
    def test_forbidden_error_is_clear_and_key_free(self, _):
        with self.assertRaises(pl_data.FetchError) as ctx:
            self.make_client().get("/a")
        self.assertIn("403", str(ctx.exception))
        self.assertNotIn("secret-key", str(ctx.exception))

    @mock.patch("ufd_app.pl_data.requests.get", side_effect=requests.exceptions.ConnectionError("secret-key leaked"))
    def test_network_errors_do_not_echo_exception_text(self, _):
        with self.assertRaises(pl_data.FetchError) as ctx:
            self.make_client().get("/a")
        self.assertNotIn("secret-key", str(ctx.exception))

    def test_missing_key_raises_before_any_request(self):
        with override_settings(FOOTBALL_DATA_API_KEY=""), mock.patch.dict("os.environ", {}, clear=True), \
             mock.patch("ufd_app.pl_data.requests.get") as get:
            client = pl_data.FootballDataClient()
            self.assertFalse(client.is_configured())
            with self.assertRaises(pl_data.FetchError):
                client.get("/a")
        get.assert_not_called()


class WriteJsonTests(SimpleTestCase):
    def test_writes_and_replaces_without_leaving_temp_files(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "sub" / "out.json"
            pl_data.write_json_atomic(path, {"a": 1})
            pl_data.write_json_atomic(path, {"a": 2, "name": "Sánchez"})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"a": 2, "name": "Sánchez"})
            self.assertEqual([p.name for p in path.parent.iterdir()], ["out.json"])

    def test_a_failed_write_keeps_the_previous_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "out.json"
            pl_data.write_json_atomic(path, {"good": True})
            with self.assertRaises(TypeError):
                pl_data.write_json_atomic(path, {"bad": object()})
            self.assertEqual(json.loads(path.read_text()), {"good": True})
            self.assertEqual([p.name for p in path.parent.iterdir()], ["out.json"])


class CommandTests(SimpleTestCase):
    def run_command(self, *args, fd=None, io=None, pitch=None):
        out = StringIO()
        with tempfile.TemporaryDirectory() as folder, \
             mock.patch.object(pl_data, "MIN_TEAMS", 3), \
             mock.patch("ufd_app.pl_data.FootballDataClient", return_value=fd or FakeFootballData()), \
             mock.patch("ufd_app.pitchapi_source.PitchApiSource", return_value=pitch or SimpleSource([], {}, configured=False)), \
             mock.patch("ufd_app.pl_data.FootballDataIoSource", return_value=io or FakeFootballDataIo()):
            target = Path(folder) / "teams.json"
            call_command("build_pl_data", "--output", str(target), "--no-history", *args, stdout=out)
            return json.loads(target.read_text(encoding="utf-8")), out.getvalue()

    def test_command_writes_the_file_and_summarises(self):
        data, output = self.run_command()
        self.assertEqual(len(data["teams"]), 3)
        self.assertIn("Wrote 3 teams (0 from PitchAPI, 3 from footballdata.io)", output)
        self.assertIn("PITCHAPI_API_KEY is not set", output)
        self.assertNotIn("apifootball", output.lower())

    def test_command_with_a_pitchapi_key_uses_it(self):
        data, output = self.run_command(pitch=full_source())
        self.assertIn("Wrote 3 teams (3 from PitchAPI, 3 from footballdata.io)", output)
        self.assertEqual(data["teams"]["61"]["stats"]["possession_source"], "pitchapi")

    def test_command_can_skip_footballdata_io(self):
        io = FakeFootballDataIo()
        data, output = self.run_command("--skip-footballdata-io", io=io)
        self.assertEqual(io.stat_calls, [])
        self.assertIn("0 from footballdata.io", output)
        self.assertFalse(any(t["sources"]["footballdata_io"] for t in data["teams"].values()))

    def test_command_without_football_data_key_fails_clearly(self):
        fd = FakeFootballData()
        fd.is_configured = lambda: False
        with self.assertRaises(CommandError):
            self.run_command(fd=fd)

    def test_command_reports_build_failures_as_errors(self):
        fd = FakeFootballData(overrides={"/competitions/PL/teams": pl_data.FetchError("down")})
        with self.assertRaises(CommandError):
            self.run_command(fd=fd)


class FootballDataIoMergeTests(SimpleTestCase):
    def test_teams_are_matched_despite_html_escaped_and_multi_season_rows(self):
        io = FakeFootballDataIo()
        data = build(io=io)
        self.assertEqual({t["footballdata_io_team_id"] for t in (v["sources"] for v in data["teams"].values())}, {901, 902, 903})
        self.assertEqual(sorted(call[0] for call in io.stat_calls), [901, 902, 903])
        # always asks for the Premier League and the current season, never the API's default competition
        self.assertTrue(all(call[1:] == (15, 103535) for call in io.stat_calls))

    def test_io_fills_possession_when_pitchapi_has_none(self):
        data = build(io=FakeFootballDataIo())
        chelsea = data["teams"]["61"]["stats"]
        self.assertEqual(chelsea["possession"], 58)
        self.assertEqual(chelsea["possession_source"], "footballdata.io")
        self.assertEqual(chelsea["possession_matches"], 5)
        self.assertEqual(chelsea["xg_for"], 7.5)
        self.assertEqual(chelsea["shots_per_match"], 14.2)
        self.assertIsNone(chelsea["corners_per_match"])  # recorded_matches was 0: no data, not "0 corners"
        self.assertEqual(data["teams"]["61"]["raw"]["footballdata_io"]["team"]["team_id"], 901)

    def test_pitchapi_possession_wins_when_both_exist(self):
        data = build(pa=full_source(), io=FakeFootballDataIo())
        stats = data["teams"]["61"]["stats"]
        self.assertEqual(stats["possession"], 58.3)
        self.assertEqual(stats["possession_source"], "pitchapi")
        self.assertEqual(stats["clean_sheets"], 2)  # football-data's own count beats footballdata.io's

    def test_zero_means_no_data(self):
        stats = build(io=FakeFootballDataIo())["teams"]["66"]["stats"]
        self.assertIsNone(stats["possession"])           # average was 0
        self.assertIsNone(stats["possession_source"])

    def test_clean_sheets_fall_back_to_io_when_football_data_matches_failed(self):
        fd = FakeFootballData(overrides={"/competitions/PL/matches": pl_data.FetchError("down")})
        data = build(fd=fd, io=FakeFootballDataIo())
        self.assertEqual(data["teams"]["61"]["stats"]["clean_sheets"], 3)

    def test_team_without_io_stats_is_still_built(self):
        data = build(io=FakeFootballDataIo())
        wolves = data["teams"]["76"]
        self.assertTrue(wolves["sources"]["footballdata_io"])
        self.assertIsNone(wolves["raw"]["footballdata_io"]["stats"])
        self.assertIsNone(wolves["stats"]["xg_for"])

    def test_io_failure_is_reported_and_the_rest_survives(self):
        io = FakeFootballDataIo(error=FootballDataIoError("monthly limit", status=429))
        data = build(pa=full_source(), io=io)
        self.assertEqual(len(io.stat_calls), 1)  # stopped at the first 429
        self.assertEqual(data["teams"]["61"]["stats"]["possession"], 58.3)
        stage = [s for s in data["meta"]["report"]["stages"] if s["name"] == "footballdata.io"][0]
        self.assertFalse(stage["ok"])

    def test_missing_key_or_league_skips_cleanly(self):
        for io in (None, FakeFootballDataIo(configured=False), FakeFootballDataIo(league_id=None)):
            data = build(io=io)
            self.assertFalse(any(t["sources"]["footballdata_io"] for t in data["teams"].values()))
            self.assertIsNone(data["teams"]["61"]["raw"]["footballdata_io"])

    def test_output_with_io_is_serialisable(self):
        json.dumps(build(pa=full_source(), io=FakeFootballDataIo()))
