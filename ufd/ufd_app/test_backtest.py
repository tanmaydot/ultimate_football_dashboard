"""Scoring and walk-forward replay: metrics are right, replays do not leak, and the model beats its baselines."""
import math
import random

from django.test import SimpleTestCase

from . import backtest as bt
from . import openfootball as of
from .predict_match import DEFAULTS, MatchModel
from .test_predict_match import synthetic
from .util import DATA_DIR


def row(probs, outcome):
    return {"probs": probs, "outcome": outcome, "date": "2020-01-01"}


class MetricTests(SimpleTestCase):
    def test_a_perfect_forecast_scores_zero(self):
        r = row((1.0, 0.0, 0.0), 0)
        self.assertEqual(bt.row_rps(r), 0.0)
        self.assertEqual(bt.row_brier(r), 0.0)
        self.assertAlmostEqual(bt.row_log_loss(r), 0.0, places=6)

    def test_known_values(self):
        r = row((0.5, 0.3, 0.2), 1)                  # a draw
        self.assertAlmostEqual(bt.row_rps(r), ((0.5 - 0) ** 2 + (0.8 - 1) ** 2) / 2)
        self.assertAlmostEqual(bt.row_brier(r), 0.25 + 0.49 + 0.04)
        self.assertAlmostEqual(bt.row_log_loss(r), -math.log(0.3))

    def test_rps_punishes_a_far_miss_more_than_a_near_one(self):
        far = bt.row_rps(row((0.8, 0.15, 0.05), 2))
        near = bt.row_rps(row((0.8, 0.15, 0.05), 1))
        self.assertGreater(far, near)

    def test_log_loss_survives_a_zero_probability(self):
        self.assertTrue(math.isfinite(bt.row_log_loss(row((1.0, 0.0, 0.0), 2))))

    def test_accuracy_and_score(self):
        rows = [row((0.6, 0.2, 0.2), 0), row((0.2, 0.2, 0.6), 0)]
        scores = bt.score(rows)
        self.assertEqual(scores["n"], 2)
        self.assertEqual(scores["accuracy"], 0.5)
        self.assertEqual(bt.score([]), {"n": 0})

    def test_outcome_index(self):
        self.assertEqual([bt.outcome_index(2, 1), bt.outcome_index(1, 1), bt.outcome_index(0, 3)], [0, 1, 2])

    def test_calibration_of_an_honest_forecaster(self):
        rng = random.Random(3)
        rows = []
        for _ in range(4000):
            p = (0.5, 0.3, 0.2)
            rows.append(row(p, rng.choices([0, 1, 2], weights=p)[0]))
        for cell in bt.calibration(rows):
            self.assertAlmostEqual(cell["predicted"], cell["observed"], delta=0.05)

    def test_paired_bootstrap_direction(self):
        better = [row((0.7, 0.2, 0.1), 0)] * 200
        worse = [row((0.4, 0.3, 0.3), 0)] * 200
        result = bt.paired_bootstrap(better, worse, resamples=200)
        self.assertLess(result["mean"], 0)
        self.assertLessEqual(result["high"], 0)

    def test_by_season_groups_by_august_to_july(self):
        rows = [dict(row((0.4, 0.3, 0.3), 0), date="2020-07-30"), dict(row((0.4, 0.3, 0.3), 0), date="2020-08-02")]
        self.assertEqual(list(bt.by_season(rows)), ["2019-20", "2020-21"])


class WalkForwardTests(SimpleTestCase):
    def setUp(self):
        self.matches = synthetic(seasons=4, teams=8, seed=21)

    def test_one_row_per_match_with_valid_probabilities(self):
        model = MatchModel(self.matches)
        rows = bt.walk_forward(model, [2020, 2021])["model"]
        self.assertEqual(len(rows), 2 * 8 * 7)
        self.assertTrue(all(abs(sum(r["probs"]) - 1) < 1e-9 for r in rows))
        self.assertEqual([r["date"] for r in rows], sorted(r["date"] for r in rows))

    def test_replay_never_uses_future_results(self):
        """Wreck every result from 2021 onwards; the 2020 forecasts must not move."""
        honest = bt.walk_forward(MatchModel(self.matches), [2020])["model"]
        wrecked = [dict(m, hg=9, ag=0) if m["date"] >= "2021-08-01" else m for m in self.matches]
        cheat = bt.walk_forward(MatchModel(wrecked), [2020])["model"]
        self.assertEqual([r["probs"] for r in honest], [r["probs"] for r in cheat])

    def test_replay_does_not_use_the_match_being_predicted(self):
        honest = bt.walk_forward(MatchModel(self.matches), [2020])["model"]
        flipped = [dict(r) for r in honest]
        target = flipped[10]
        altered = [dict(m, hg=m["ag"] + 6, ag=0) if (m["date"], m["home"], m["away"]) == (target["date"], target["home"], target["away"]) else m
                   for m in self.matches]
        again = bt.walk_forward(MatchModel(altered), [2020])["model"]
        self.assertEqual(honest[10]["probs"], again[10]["probs"])

    def test_variants_share_the_fit(self):
        runs = bt.walk_forward(MatchModel(self.matches), [2021], {"a": {}, "b": {"form_weight": 0.5}})
        self.assertEqual(len(runs["a"]), len(runs["b"]))
        self.assertNotEqual([r["probs"] for r in runs["a"]], [r["probs"] for r in runs["b"]])

    def test_climatology_uses_only_earlier_matches(self):
        model = MatchModel(self.matches)
        rows = bt.walk_forward(model, [2021])["model"]                    # the 2021-22 season
        base = bt.climatology(model, rows)
        after = [dict(m, hg=9, ag=0) if m["date"] >= "2022-08-01" else m for m in self.matches]    # wreck what comes after it
        wrecked = bt.climatology(MatchModel(after), rows)
        self.assertEqual([r["probs"] for r in base], [r["probs"] for r in wrecked])
        self.assertTrue(all(abs(sum(r["probs"]) - 1) < 1e-9 for r in base))

    def test_on_a_league_with_real_differences_the_model_beats_the_constant_forecast(self):
        matches = synthetic(seasons=5, teams=10, seed=8)
        report, _ = bt.evaluate(matches, DEFAULTS, [2021, 2022])
        scores = report["scores"]
        self.assertLess(scores["model"]["rps"], scores["constant_home_draw_away_rates"]["rps"])
        self.assertLess(report["rps_difference_vs"]["constant_home_draw_away_rates"]["high"], 0)
        self.assertIn("per_season", report)

    def test_on_a_league_where_all_clubs_are_equal_nothing_is_gained(self):
        equal = {f"t{i}": (1.0, 1.0) for i in range(10)}
        matches = synthetic(seasons=5, teams=10, seed=8, strengths=equal)
        report, _ = bt.evaluate(matches, DEFAULTS, [2021, 2022])
        gap = report["scores"]["model"]["rps"] - report["scores"]["constant_home_draw_away_rates"]["rps"]
        self.assertGreater(gap, -0.01)       # no invented skill

    def test_goal_error_summary(self):
        rows = bt.walk_forward(MatchModel(self.matches), [2021])["model"]
        summary = bt.goal_error(rows)
        self.assertAlmostEqual(summary["mean_predicted_home_goals"], summary["mean_actual_home_goals"], delta=0.5)


class RealDataRegressionTests(SimpleTestCase):
    """Two real seasons replayed from the committed archive: the claims in explain.md stay true."""

    def test_model_beats_the_baseline_on_recent_real_seasons(self):
        matches = of.Archive.load(DATA_DIR / "openfootball.json").played()
        model = MatchModel(matches)
        rows = bt.walk_forward(model, [2022, 2023])["model"]
        base = bt.climatology(model, rows)
        ours, theirs = bt.score(rows), bt.score(base)
        self.assertEqual(ours["n"], 760)
        self.assertLess(ours["rps"], theirs["rps"] - 0.015)
        self.assertGreater(ours["accuracy"], 0.5)
        self.assertLess(ours["log_loss"], theirs["log_loss"])
        for cell in bt.calibration(rows, bins=5):
            if cell["n"] >= 100:
                self.assertAlmostEqual(cell["predicted"], cell["observed"], delta=0.07)
