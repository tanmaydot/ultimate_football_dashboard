"""build_predictions, backtest_predictions and the data checker's new rules."""
import json
import tempfile
from io import StringIO
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase

from . import validation
from .management.commands.check_data import check_files
from .openfootball import Archive
from .test_predictions import TODAY, build, match, world


class Files:
    def __init__(self, test):
        self.tmp = tempfile.TemporaryDirectory()
        test.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        pl, history, archive = world()
        self.pl, self.history, self.archive = self.dir / "pl.json", self.dir / "h.json", self.dir / "o.json"
        self.pl.write_text(json.dumps(pl), encoding="utf-8")
        self.history.write_text(json.dumps(history), encoding="utf-8")
        archive.save(self.archive)
        self.out = self.dir / "pred.json"
        self.validation = self.dir / "val.json"

    def args(self, *extra):
        return ["--output", str(self.out), "--archive", str(self.archive), "--pl-data", str(self.pl), "--history", str(self.history),
                "--validation", str(self.validation), *extra]


class BuildCommandTests(SimpleTestCase):
    def run_command(self, *args):
        out = StringIO()
        call_command("build_predictions", *args, stdout=out)
        return out.getvalue()

    def test_offline_build_writes_a_valid_file(self):
        f = Files(self)
        text = self.run_command(*f.args("--offline"))
        doc = json.loads(f.out.read_text(encoding="utf-8"))
        self.assertIn("fixture forecasts", text)
        self.assertEqual(len(doc["fixtures"]), 3)
        self.assertIsNone(doc["meta"]["validation"])

    def test_a_validation_report_is_summarised_inside_the_output(self):
        f = Files(self)
        f.validation.write_text(json.dumps({"generated_at": "x", "team_model": {
            "test_seasons": ["2016-17", "2026-27"], "matches": 10,
            "scores": {"model": {"accuracy": 0.5, "rps": 0.2, "log_loss": 1.0},
                       "constant_home_draw_away_rates": {"accuracy": 0.4, "rps": 0.23}}}}), encoding="utf-8")
        self.run_command(*f.args("--offline"))
        self.assertEqual(json.loads(f.out.read_text(encoding="utf-8"))["meta"]["validation"]["baseline_accuracy"], 0.4)

    def test_online_build_downloads_then_saves_the_archive(self):
        f = Files(self)
        with mock.patch.object(Archive, "update", return_value=["2026-27"]) as update, \
                mock.patch.object(Archive, "save") as save:
            text = self.run_command(*f.args())
        update.assert_called_once()
        save.assert_called_once()
        self.assertIn("1 season(s) downloaded", text)

    def test_nothing_downloaded_means_nothing_saved(self):
        f = Files(self)
        with mock.patch.object(Archive, "update", return_value=[]), mock.patch.object(Archive, "save") as save:
            self.run_command(*f.args())
        save.assert_not_called()

    def test_without_any_openfootball_data_it_explains(self):
        f = Files(self)
        f.archive.unlink()
        with self.assertRaisesRegex(CommandError, "OpenFootball"):
            self.run_command(*f.args("--offline"))

    def test_without_the_project_data_it_says_what_to_run_first(self):
        f = Files(self)
        f.pl.unlink()
        with self.assertRaisesRegex(CommandError, "build_pl_data"):
            self.run_command(*f.args("--offline"))


def multi_season_world():
    """Three clubs over 2010-2026 so the backtest has a test period."""
    pl, history, archive = world()
    keys = [m["home"] for m in archive.seasons["2024-25"]][:1] + [m["away"] for m in archive.seasons["2024-25"]][:2]
    keys = sorted({m["home"] for m in archive.seasons["2024-25"]})
    for year in range(2010, 2024):
        rows, n = [], 0
        for h in keys:
            for a in keys:
                if h != a:
                    n += 1
                    rows.append(match(f"{year}-{9 + n // 4:02d}-{10 + n:02d}", h, a, (n + year) % 4, (n * 2 + year) % 3))
        archive.seasons[f"{year}-{(year + 1) % 100:02d}"] = rows
    return pl, history, archive


class BacktestCommandTests(SimpleTestCase):
    def test_the_report_has_both_models_and_honest_notes(self):
        pl, history, archive = multi_season_world()
        report = validation.run(pl, history, archive, today=TODAY)
        team = report["team_model"]
        self.assertGreater(team["matches"], 20)
        self.assertEqual(team["test_seasons"][0], "2016-17")
        for key in ("scores", "rps_difference_vs", "per_season", "calibration", "goals", "tuned_on_seasons", "params"):
            self.assertIn(key, team)
        self.assertIn("constant_home_draw_away_rates", team["scores"])
        self.assertIsNone(report["player_model"])                  # only two matches of player data: too few to validate
        self.assertTrue(any("odds" in n for n in report["notes"]))
        json.dumps(report)

    def test_the_player_report_appears_once_there_is_enough_data(self):
        from .test_predict_player import league
        matches, positions = league(30)
        matches = [{**m, "home": 61, "away": 66, "teams": {61: m["teams"][1]}} for m in matches]       # club ids from the shared test data
        pl, history, archive = world()
        played = [m for ms in archive.seasons.values() for m in ms if m["hg"] is not None]
        with mock.patch.object(validation, "player_inputs", return_value=(matches, positions, {})):
            player = validation.player_validation(pl, history, played, None)
        self.assertEqual(player["matches"], 30)
        self.assertGreater(player["holdout_window"]["scorers"], 0)
        self.assertIn("season_totals_only", player["holdout_window"]["variants"])
        self.assertIn("position_average", player["holdout_window"]["baselines"])

    def test_command_writes_the_file_and_reports(self):
        f = Files(self)
        _pl, _h, archive = multi_season_world()
        archive.save(f.archive)
        out = StringIO()
        call_command("backtest_predictions", "--output", str(f.validation), "--archive", str(f.archive),
                     "--pl-data", str(f.pl), "--history", str(f.history), stdout=out)
        self.assertIn("Match model on", out.getvalue())
        self.assertIn("team_model", json.loads(f.validation.read_text(encoding="utf-8")))

    def test_command_without_data_explains(self):
        f = Files(self)
        f.archive.unlink()
        with self.assertRaisesRegex(CommandError, "build_predictions"):
            call_command("backtest_predictions", "--archive", str(f.archive), "--pl-data", str(f.pl), "--history", str(f.history), "--output", str(f.validation))


class CheckerTests(SimpleTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        teams = {"meta": {"generated_at": "x"}, "standings": [], "teams": {str(i): {} for i in range(20)}}
        history = {"results": {}, "team_stats": {}, "player_stats": {}, "pitch_matches": {}}
        (self.dir / "t.json").write_text(json.dumps(teams), encoding="utf-8")
        (self.dir / "h.json").write_text(json.dumps(history), encoding="utf-8")

    def check(self, **kw):
        return check_files(self.dir / "t.json", self.dir / "h.json", **kw)

    def test_absent_optional_files_are_fine(self):
        self.assertEqual(self.check(predictions_path=self.dir / "nope.json", archive_path=self.dir / "nope2.json"), [])

    def test_predictions_that_do_not_add_up_are_caught(self):
        doc = build()
        doc["fixtures"][0]["probs"]["home"] = 0.9
        (self.dir / "p.json").write_text(json.dumps(doc), encoding="utf-8")
        problems = self.check(predictions_path=self.dir / "p.json")
        self.assertTrue(any("do not add up" in p for p in problems))

    def test_good_predictions_pass_and_a_conflict_marker_is_caught(self):
        (self.dir / "p.json").write_text(json.dumps(build()), encoding="utf-8")
        self.assertEqual(self.check(predictions_path=self.dir / "p.json"), [])
        (self.dir / "p.json").write_text("<<<<<<< HEAD\n{}\n=======\n{}\n>>>>>>> x\n", encoding="utf-8")
        self.assertTrue(any("conflict markers" in p for p in self.check(predictions_path=self.dir / "p.json")))

    def test_an_incomplete_finished_season_in_the_archive_is_caught(self):
        rows = [["2020-09-01", 1, "a", "b", 1, 0]] * 10
        (self.dir / "o.json").write_text(json.dumps({"columns": [], "seasons": {"2020-21": rows}}), encoding="utf-8")
        problems = self.check(archive_path=self.dir / "o.json")
        self.assertTrue(any("expected 380" in p for p in problems))
        (self.dir / "o.json").write_text(json.dumps({"seasons": {}}), encoding="utf-8")
        self.assertTrue(any("no seasons" in p for p in self.check(archive_path=self.dir / "o.json")))
