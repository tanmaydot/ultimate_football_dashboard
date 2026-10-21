"""The predictions builder, the page helpers and the pages that show them."""
import json
import tempfile
from datetime import date
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from . import dashboard, predictions
from .openfootball import Archive
from .test_dashboard import built_data

TODAY = date(2026, 10, 20)
NOW = "2026-10-20T12:00:00+00:00"


def match(day, home, away, hg=None, ag=None, matchday=None):
    return {"date": day, "matchday": matchday, "home": home, "away": away, "hg": hg, "ag": ag}


def world(history_extra=None):
    """Three clubs from the shared test data, two finished seasons, a season in progress with fixtures to come."""
    pl = built_data()
    by_id, by_key = predictions.team_directory(pl)
    keys = [by_id[i]["key"] for i in (61, 66, 76)]
    seasons = {}
    for year in (2024, 2025):
        rows, n = [], 0
        for h in keys:
            for a in keys:
                if h != a:
                    n += 1
                    rows.append(match(f"{year}-{9 + n // 4:02d}-{10 + n:02d}", h, a, (n * 3) % 4, (n * 5) % 3))
        seasons[f"{year}-{(year + 1) % 100:02d}"] = rows
    c, m, w = keys
    seasons["2026-27"] = [
        match("2026-08-15", c, m, 2, 0, 1), match("2026-09-12", w, c, 0, 1, 2),
        match("2026-10-03", m, w, None, None, 4),                  # OpenFootball has no result yet; history does (r1 below)
        match("2026-10-19", w, m, None, None, 5),                  # dated before today with no result anywhere: awaiting
        match("2026-10-24", c, w, None, None, 6), match("2026-11-07", m, c, None, None, 8)]
    history = {"results": {
        "r1": {"date": "2026-10-03", "home": 66, "away": 76, "home_goals": 1, "away_goals": 1, "matchday": 4}},
        "pitch_matches": {
            "x1": {"event": {"match_id": "x1", "match_date": "2026-08-15", "match_hometeam_id": "t_che", "match_awayteam_id": "t_mun"},
                   "players": {"p_t_chek": {"t": "t_che", "min": 90, "rt": 6.9}, "p_t_chew": {"t": "t_che", "min": 90, "g": 2, "xg": 1.1, "rt": 8.4}}},
            "x2": {"event": {"match_id": "x2", "match_date": "2026-09-12", "match_hometeam_id": "t_wol", "match_awayteam_id": "t_che"},
                   "players": {"p_t_chek": {"t": "t_che", "min": 90, "rt": 7.1}, "p_t_chew": {"t": "t_che", "min": 80, "a": 1, "rt": 7.7}}}}}
    history.update(history_extra or {})
    return pl, history, Archive(seasons)


def ids(pl):
    return {tid: info["key"] for tid, info in predictions.team_directory(pl)[0].items()}


def build(**kw):
    pl, history, archive = world()
    return predictions.build_predictions(pl, history, archive, today=TODAY, now=NOW, **kw)


class MergeTests(SimpleTestCase):
    def test_project_results_fill_in_what_openfootball_has_not_got_yet(self):
        pl, history, archive = world()
        played, fixtures, added = predictions.merge_current_season(archive, history["results"], ids(pl), TODAY)
        self.assertEqual(added, 1)
        self.assertEqual([f["date"] for f in fixtures], ["2026-10-19", "2026-10-24", "2026-11-07"])
        self.assertIn(("2026-10-03", 1, 1), {(m["date"], m["hg"], m["ag"]) for m in played})

    def test_where_both_have_a_result_the_project_wins(self):
        pl, history, archive = world()
        history["results"]["r2"] = {"date": "2026-08-15", "home": 61, "away": 66, "home_goals": 3, "away_goals": 3, "matchday": 1}
        played, _f, added = predictions.merge_current_season(archive, history["results"], ids(pl), TODAY)
        self.assertEqual(added, 1)
        self.assertEqual([m["hg"] for m in played if m["date"] == "2026-08-15"], [3])

    def test_a_result_for_an_unknown_club_is_skipped(self):
        pl, history, archive = world()
        history["results"]["r3"] = {"date": "2026-09-01", "home": 999, "away": 61, "home_goals": 1, "away_goals": 0}
        self.assertEqual(predictions.merge_current_season(archive, history["results"], ids(pl), TODAY)[2], 1)

    def test_a_fixture_with_a_result_is_not_left_in_the_fixture_list(self):
        pl, history, archive = world()
        history["results"]["r4"] = {"date": "2026-10-24", "home": 61, "away": 76, "home_goals": 2, "away_goals": 2, "matchday": 6}
        _p, fixtures, _a = predictions.merge_current_season(archive, history["results"], ids(pl), TODAY)
        self.assertNotIn("2026-10-24", [f["date"] for f in fixtures])


class BuildTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.doc = build()

    def test_shape(self):
        self.assertEqual(self.doc["meta"]["schema_version"], predictions.SCHEMA_VERSION)
        self.assertEqual(self.doc["meta"]["season"], "2026-27")
        self.assertEqual(set(self.doc), {"meta", "fixtures", "teams", "players"})
        self.assertEqual(set(self.doc["teams"]), {"61", "66", "76"})
        self.assertTrue(self.doc["meta"]["not_available"])

    def test_every_fixture_has_probabilities_that_add_up(self):
        self.assertEqual(len(self.doc["fixtures"]), 3)
        for f in self.doc["fixtures"]:
            self.assertAlmostEqual(sum(f["probs"].values()), 1.0, places=2)
            self.assertAlmostEqual(f["h2h"]["n"], f["h2h"]["n"])
            self.assertTrue(f["insights"])
            self.assertEqual(len(f["top_scores"]), 3)
            self.assertTrue(all(0 <= f[k] <= 1 for k in ("over_2_5", "btts")))
        self.assertEqual([f["date"] for f in self.doc["fixtures"]], sorted(f["date"] for f in self.doc["fixtures"]))

    def test_an_overdue_fixture_is_marked_not_dropped(self):
        flags = {f["date"]: f["awaiting_result"] for f in self.doc["fixtures"]}
        self.assertTrue(flags["2026-10-19"])
        self.assertFalse(flags["2026-10-24"])

    def test_players_are_forecast_for_the_next_real_fixture_only(self):
        chelsea = self.doc["players"]["61"]
        self.assertEqual(set(chelsea), {"611", "612"})                          # 613 has no PitchAPI link
        winger = chelsea["612"]
        self.assertEqual((winger["next"]["date"], winger["next"]["opponent_id"]), ("2026-10-24", 76))
        self.assertTrue(winger["next"]["home"])
        self.assertEqual(winger["last"]["assists"], 1)
        self.assertEqual(winger["appearances"], 2)
        self.assertEqual(self.doc["meta"]["report"]["players"]["squad_players_without_match_data"], 7)

    def test_it_is_deterministic_and_serialisable(self):
        again = build()
        self.assertEqual(json.dumps(self.doc, sort_keys=True), json.dumps(again, sort_keys=True))

    def test_the_team_block_has_form_and_strength(self):
        team = self.doc["teams"]["61"]
        self.assertTrue(team["form"]["matches"])
        self.assertLessEqual(len(team["form"]["matches"]), predictions.FORM_LENGTH)
        self.assertEqual(sorted(t["strength"]["attack_rank"] for t in self.doc["teams"].values()), [1, 2, 3])

    def test_the_validation_summary_is_embedded_when_given(self):
        self.assertIsNone(self.doc["meta"]["validation"])
        report = {"generated_at": "x", "team_model": {"test_seasons": ["2016-17", "2026-27"], "matches": 3879,
                                                      "scores": {"model": {"accuracy": 0.54, "rps": 0.199, "log_loss": 0.97},
                                                                 "constant_home_draw_away_rates": {"accuracy": 0.445, "rps": 0.233}}}}
        summary = build(validation=report)["meta"]["validation"]
        self.assertEqual((summary["accuracy"], summary["baseline_accuracy"], summary["matches"]), (0.54, 0.445, 3879))

    def test_a_season_with_nothing_left_to_play_still_builds(self):
        pl, history, archive = world()
        archive.seasons["2026-27"] = [m for m in archive.seasons["2026-27"] if m["hg"] is not None]
        doc = predictions.build_predictions(pl, history, archive, today=TODAY, now=NOW)
        self.assertEqual(doc["fixtures"], [])
        self.assertEqual(doc["players"], {})

    def test_no_history_file_still_builds(self):
        pl, _history, archive = world()
        doc = predictions.build_predictions(pl, {}, archive, today=TODAY, now=NOW)
        self.assertEqual(len(doc["fixtures"]), 4)               # the 3 October match has a result only in the history file
        self.assertEqual(doc["players"].get("61", {}), {})

    def test_a_fixture_with_an_unknown_club_is_reported_not_forecast(self):
        pl, history, archive = world()
        archive.seasons["2026-27"].append(match("2026-12-01", "ghost united", "chelsea", None, None, 9))
        doc = predictions.build_predictions(pl, history, archive, today=TODAY, now=NOW)
        self.assertEqual(len(doc["fixtures"]), 3)
        self.assertEqual(doc["meta"]["report"]["unmatched_fixtures"], ["ghost united v chelsea"])


class HelperTests(SimpleTestCase):
    def test_percentages_always_add_up_to_100(self):
        for probs in ({"home": 0.333, "draw": 0.333, "away": 0.334}, {"home": 0.5, "draw": 0.25, "away": 0.25},
                      {"home": 0.4549, "draw": 0.2726, "away": 0.2725}, {"home": 0.999, "draw": 0.0005, "away": 0.0005}):
            self.assertEqual(sum(predictions.percentages(probs).values()), 100, probs)

    def test_next_matchday_and_upcoming_skip_overdue_fixtures(self):
        doc = build()
        self.assertEqual([f["date"] for f in predictions.upcoming(doc)], ["2026-10-24", "2026-11-07"])
        self.assertEqual({f["matchday"] for f in predictions.next_matchday(doc)}, {6})
        self.assertEqual(len(predictions.upcoming(doc, 76)), 1)
        self.assertEqual(len(predictions.awaiting_result(doc, 66)), 1)
        self.assertEqual(predictions.upcoming(None), [])

    def test_decorate_fixture_gives_the_clubs_point_of_view(self):
        doc = build()
        fixture = predictions.upcoming(doc, 61)[0]
        teams = {61: {"id": 61, "name": "Chelsea FC", "shortName": "Chelsea", "tla": "CHE", "crest": None}}
        view = predictions.decorate_fixture(fixture, teams, 61)
        self.assertTrue(view["at_home"])
        self.assertEqual(view["own_pct"]["win"], view["pct"]["home"])
        self.assertEqual(view["opponent"]["name"], "Unknown club")                  # not in the lookup: still renderable
        away = predictions.decorate_fixture(predictions.upcoming(doc, 61)[1], teams, 61)
        self.assertFalse(away["at_home"])
        self.assertEqual(away["own_pct"]["win"], away["pct"]["away"])

    def test_insights_cover_favourites_close_games_form_h2h_and_caveats(self):
        def pred(h, d, a, newcomer=False, h2h=None):
            return {"probs": {"home": h, "draw": d, "away": a}, "xg": {"home": 2.0, "away": 0.8},
                    "newcomer": {"home": newcomer, "away": False}, "h2h": h2h}
        grid = [[0.0] * 9 for _ in range(9)]
        grid[2][1] = 1.0
        summary = predictions.scoreline_summary(grid)
        forms = {"home": {"matches": [{"result": "W"}] * 5, "gf": 9, "ga": 2}, "away": {"matches": [{"result": "L"}] * 5, "gf": 1, "ga": 9}}
        notes = predictions.match_insights("Home", "Away", pred(0.7, 0.2, 0.1, True,
                                           {"n": 4, "record": {"home_wins": 3, "draws": 1, "away_wins": 0}}), summary, forms, [3, 9])
        text = " ".join(n["text"] for n in notes)
        self.assertIn("strong favourites (70%", text)
        self.assertIn("unbeaten in their last 5", text)
        self.assertIn("without a win in their last 5", text)
        self.assertIn("Last 4 meetings", text)
        self.assertIn("came up this season", text)
        self.assertIn("Only 3 league games", text)
        close = predictions.match_insights("Home", "Away", pred(0.36, 0.28, 0.36), summary, forms, [10, 10])
        self.assertTrue(any(n["kind"] == "close" for n in close))
        self.assertIn("1 win, 1 draw and 1 defeat", predictions._record_words([{"result": "W"}, {"result": "D"}, {"result": "L"}]))


class LoaderTests(SimpleTestCase):
    def test_missing_corrupt_and_wrong_shape_files_give_none(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "p.json"
            self.assertIsNone(predictions.load_predictions(path))
            path.write_text("{broken", encoding="utf-8")
            self.assertIsNone(predictions.load_predictions(path))
            path.write_text("[1, 2]", encoding="utf-8")
            self.assertIsNone(predictions.load_predictions(path))

    def test_it_reloads_when_the_file_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "p.json"
            path.write_text(json.dumps({"fixtures": [], "meta": {"v": 1}}), encoding="utf-8")
            self.assertEqual(predictions.load_predictions(path)["meta"]["v"], 1)
            path.write_text(json.dumps({"fixtures": [], "meta": {"v": 2}}), encoding="utf-8")
            import os
            os.utime(path, (path.stat().st_atime, path.stat().st_mtime + 5))
            self.assertEqual(predictions.load_predictions(path)["meta"]["v"], 2)


class PageTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "pl.json"
        self.pl = built_data()
        self.path.write_text(json.dumps(self.pl), encoding="utf-8")
        self.doc = build()
        self.doc_patch = mock.patch.object(predictions, "load_predictions", return_value=self.doc)
        for patcher in (mock.patch.object(dashboard, "PL_DATA_PATH", self.path), mock.patch.object(dashboard, "load_club_extras", return_value={}),
                        self.doc_patch):
            patcher.start()
            self.addCleanup(patcher.stop)
        dashboard._data_cache.update(path=None, mtime=None, data=None)

    def page(self, name="predictions", *args):
        return self.client.get(reverse(f"ufd_app:{name}", args=args or [61]))

    def test_the_predictions_page_shows_the_next_match(self):
        response = self.page()
        self.assertEqual(response.status_code, 200)
        nxt = response.context["next_match"]
        self.assertEqual((nxt["date"], nxt["opponent"]["name"], nxt["at_home"]), ("2026-10-24", "Wolverhampton Wanderers FC", True))
        self.assertEqual(sum(nxt["pct"].values()), 100)
        for text in ("Next match", "Match insights", "Previous meetings", "Following fixtures", "Team strength", "Recent form", "How these numbers are made"):
            self.assertContains(response, text)
        self.assertContains(response, 'role="img"')                                # the probability bar is described for screen readers
        self.assertContains(response, 'aria-label="Home win')

    def test_other_tabs_and_lists(self):
        context = self.page().context
        self.assertEqual([f["matchday"] for f in context["league_fixtures"]], [6])
        self.assertEqual(len(context["fixtures"]), 1)                              # the one after the next match (away at Man United)
        self.assertEqual([p["id"] for p in context["players"]][0] in (611, 612), True)
        self.assertEqual(context["players_missing"], 1)
        eithers = [p["o"]["p_goal_or_assist"] for p in context["players"]]
        self.assertEqual(eithers, sorted(eithers, reverse=True))
        self.assertContains(self.page(), "outlookTable")

    def test_an_overdue_fixture_is_explained(self):
        self.assertContains(self.page("predictions", 66), "Waiting for a result")

    def test_without_a_predictions_file_the_page_says_how_to_build_it(self):
        self.doc_patch.stop()
        with mock.patch.object(predictions, "load_predictions", return_value=None):
            response = self.page()
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "build_predictions")
            self.assertNotContains(response, "Match insights")
            self.assertContains(self.page("dashboard"), "No forecast yet")
        self.doc_patch.start()

    def test_unknown_club_is_friendly(self):
        response = self.page("predictions", 999)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "isn&#x27;t in the Premier League data")

    def test_the_sidebar_and_nav_link_to_predictions(self):
        html = self.page().content.decode()
        self.assertIn(f'data-href="{reverse("ufd_app:predictions", args=[66])}"', html)
        self.assertIn(reverse("ufd_app:predictions", args=[61]), self.page("squad").content.decode())

    def test_the_squad_page_gets_outlook_columns(self):
        response = self.page("squad")
        self.assertTrue(response.context["has_outlook"])
        self.assertContains(response, "Goal %")
        self.assertContains(response, "Exp. rating")
        rows = {p["id"]: p for p in response.context["players"]}
        self.assertIsNotNone(rows[612]["outlook"])
        self.assertIsNone(rows[613]["outlook"])
        self.assertContains(response, "—")

    def test_the_squad_page_without_predictions_is_unchanged(self):
        self.doc_patch.stop()
        with mock.patch.object(predictions, "load_predictions", return_value=None):
            response = self.page("squad")
            self.assertFalse(response.context["has_outlook"])
            self.assertNotContains(response, "Exp. rating")
        self.doc_patch.start()

    def test_the_player_page_shows_the_outlook_only_when_there_is_one(self):
        with_outlook = self.page("player", 61, 612)
        self.assertContains(with_outlook, "Next match outlook")
        self.assertEqual(with_outlook.context["outlook"]["opponent"]["name"], "Wolverhampton Wanderers FC")
        self.assertNotContains(self.page("player", 61, 613), "Next match outlook")

    def test_the_dashboard_widget_carries_the_forecast(self):
        response = self.page("dashboard")
        self.assertEqual(response.context["prediction"]["opponent"]["name"], "Wolverhampton Wanderers FC")
        self.assertContains(response, 'id="tpl-prediction"')
        self.assertContains(response, "Most likely")
        layout = {w["id"]: w for w in response.context["default_layout"]}
        self.assertEqual(layout["prediction"]["url"], reverse("ufd_app:predictions", args=[61]))
        self.assertFalse(layout["prediction"].get("hidden"))
