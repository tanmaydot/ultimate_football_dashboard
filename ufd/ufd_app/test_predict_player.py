"""The player model: shrinkage, minutes, context, ratings, leakage and the validation helpers."""
import math
import random

from django.test import SimpleTestCase

from . import predict_player as pp
from .predict_player import PlayerTracker


def line(minutes, g=0, a=0, xg=0.0, xa=0.0, rt=None, sh=0):
    out = {"min": minutes, "g": g, "a": a, "xg": xg, "xa": xa, "sh": sh}
    if rt is not None:
        out["rt"] = rt
    return out


def match(day, lines, team=1):
    return {"date": f"2026-09-{day:02d}", "teams": {team: lines}}


def filler_match(day, scorers=(), team=1):
    """A match with ten reliable outfield players (the 'rest of the league' that sets the position averages)."""
    lines = {f"f{i}": line(90, g=1 if i in scorers else 0, rt=6.6 + 0.05 * i) for i in range(10)}
    return match(day, lines, team)


POSITIONS = {**{f"f{i}": "Midfield" for i in range(10)}, "star": "Offence", "keeper": "Goalkeeper", "sub": "Offence"}


def tracker_after(matches, params=None, positions=POSITIONS):
    t = PlayerTracker(positions, params)
    for m in matches:
        t.observe(m)
    return t


class RateTests(SimpleTestCase):
    def test_one_goal_in_one_game_is_shrunk_towards_the_position_average(self):
        matches = [match(d, {**filler_match(d)["teams"][1], "star": line(90, g=1 if d == 1 else 0)}) for d in range(1, 8)]
        t = tracker_after(matches, {"xg_weight": 0.0, "recent_weight": 0.0})
        rate = t.predict("star")["goals_per_90"]
        raw = 1 / 7
        self.assertGreater(rate, 0)
        self.assertLess(abs(rate - t._prior("Offence", "g")), abs(raw - t._prior("Offence", "g")) + 1e-9)

    def test_a_goal_in_a_five_minute_cameo_is_not_taken_at_face_value(self):
        matches = [match(1, {**filler_match(1)["teams"][1], "sub": line(5, g=1)})] + [filler_match(d) for d in range(2, 7)]
        t = tracker_after(matches, {"xg_weight": 0.0, "recent_weight": 0.0})
        prediction = t.predict("sub")
        self.assertLess(prediction["goals_per_90"], 1.0)                  # the raw figure is 18 goals per 90
        self.assertLess(prediction["p_goal"], 0.1)                        # and he is hardly expected to play

    def test_more_goals_over_the_same_minutes_gives_a_higher_rate(self):
        base = [filler_match(d) for d in range(1, 9)]
        low = tracker_after([match(d, {**m["teams"][1], "star": line(90, g=0)}) for d, m in enumerate(base, 1)])
        high = tracker_after([match(d, {**m["teams"][1], "star": line(90, g=1 if d % 2 else 0)}) for d, m in enumerate(base, 1)])
        self.assertGreater(high.predict("star")["goals_per_90"], low.predict("star")["goals_per_90"])

    def test_xg_pulls_the_rate_up_for_a_player_who_missed_good_chances(self):
        base = [filler_match(d, scorers=(1, 2)) for d in range(1, 9)]          # enough goals in the league to rescale xG
        matches = [match(d, {**m["teams"][1], "star": line(90, g=0, xg=0.6)}) for d, m in enumerate(base, 1)]
        goals_only = tracker_after(matches, {"xg_weight": 0.0}).predict("star")["goals_per_90"]
        with_xg = tracker_after(matches, {"xg_weight": 0.8}).predict("star")["goals_per_90"]
        self.assertGreater(with_xg, goals_only)

    def test_the_most_recent_match_counts_when_the_recent_weight_is_on(self):
        base = [filler_match(d) for d in range(1, 9)]
        hot = [match(d, {**m["teams"][1], "star": line(90, g=2 if d >= 7 else 0)}) for d, m in enumerate(base, 1)]
        off = tracker_after(hot, {"recent_weight": 0.0, "xg_weight": 0.0}).predict("star")["goals_per_90"]
        on = tracker_after(hot, {"recent_weight": 0.5, "xg_weight": 0.0}).predict("star")["goals_per_90"]
        self.assertGreater(on, off)


class MinutesTests(SimpleTestCase):
    def test_an_unused_or_absent_match_counts_as_zero_minutes(self):
        matches = [match(d, {**filler_match(d)["teams"][1], "star": line(90)}) for d in range(1, 4)]
        matches += [filler_match(d) for d in range(4, 7)]                   # star is not in the squad list any more
        t = tracker_after(matches)
        regular = tracker_after([match(d, {**filler_match(d)["teams"][1], "star": line(90)}) for d in range(1, 7)])
        self.assertLess(t.predict("star")["expected_minutes"], regular.predict("star")["expected_minutes"])
        self.assertAlmostEqual(regular.predict("star")["expected_minutes"], 90, places=6)
        self.assertEqual(regular.predict("star")["p_play"], 1.0)

    def test_recent_minutes_matter_more_than_old_ones(self):
        returning = [match(d, {**filler_match(d)["teams"][1], "star": line(90 if d > 4 else 0)}) for d in range(1, 9)]
        fading = [match(d, {**filler_match(d)["teams"][1], "star": line(0 if d > 4 else 90)}) for d in range(1, 9)]
        self.assertGreater(tracker_after(returning).predict("star")["expected_minutes"],
                           tracker_after(fading).predict("star")["expected_minutes"])

    def test_a_player_who_has_never_played_has_no_prediction(self):
        self.assertIsNone(tracker_after([filler_match(1)]).predict("nobody"))

    def test_a_transfer_moves_the_player_to_the_new_club(self):
        t = tracker_after([match(1, {"x": line(90)}, team=1), match(2, {"x": line(90)}, team=2)])
        self.assertEqual(t.players["x"].team, 2)


class ProbabilityTests(SimpleTestCase):
    def setUp(self):
        base = [filler_match(d) for d in range(1, 13)]                           # 12 x 90 minutes: enough for "high" confidence
        self.tracker = tracker_after([match(d, {**m["teams"][1], "star": line(90, g=1 if d % 3 == 0 else 0, a=1 if d % 4 == 0 else 0, rt=7.2)})
                                      for d, m in enumerate(base, 1)])

    def test_probabilities_are_valid_and_ordered(self):
        p = self.tracker.predict("star")
        for key in ("p_goal", "p_assist", "p_goal_or_assist"):
            self.assertTrue(0 <= p[key] <= 1)
        self.assertGreaterEqual(p["p_goal_or_assist"], max(p["p_goal"], p["p_assist"]))
        self.assertAlmostEqual(p["p_goal"], 1 - math.exp(-p["expected_goals"]), places=9)

    def test_a_leakier_opponent_and_a_home_venue_raise_the_chance(self):
        tough = self.tracker.predict("star", opponent_defence=0.7)["p_goal"]
        weak = self.tracker.predict("star", opponent_defence=1.4)["p_goal"]
        self.assertGreater(weak, tough)
        self.assertEqual(self.tracker.predict("star", 1.0, 1.0)["p_goal"], self.tracker.predict("star")["p_goal"])

    def test_context_can_be_switched_off(self):
        off = PlayerTracker(POSITIONS, {"context_exponent": 0.0})
        for m in [match(d, {"star": line(90, g=d % 2)}) for d in range(1, 6)]:
            off.observe(m)
        self.assertEqual(off.predict("star", 0.5)["p_goal"], off.predict("star", 1.5)["p_goal"])

    def test_confidence_follows_minutes(self):
        self.assertEqual(self.tracker.predict("star")["confidence"], "high")
        short = tracker_after([match(1, {"star": line(45)})])
        self.assertEqual(short.predict("star")["confidence"], "low")

    def test_last_game(self):
        self.assertEqual(self.tracker.last_game("star")[0], 90)
        self.assertIsNone(self.tracker.last_game("nobody"))


class RatingTests(SimpleTestCase):
    def rated(self, ratings, minutes=90, params=None):
        return tracker_after([match(d + 1, {"star": line(minutes, rt=r), **{f"f{i}": line(90, rt=6.8) for i in range(10)}})
                              for d, r in enumerate(ratings)], params)

    def test_the_rating_is_pulled_towards_the_position_average(self):
        t = self.rated([9.5])
        value = t.predict("star")["expected_rating"]
        self.assertLess(value, 9.5)
        self.assertGreater(value, t._rating_prior("Offence"))

    def test_a_cameo_has_no_rating_and_a_player_without_ratings_has_none(self):
        self.assertIsNone(self.rated([8.0], minutes=10).predict("star")["expected_rating"])

    def test_hot_cold_and_steady(self):
        self.assertEqual(self.rated([6.0, 6.0, 6.0, 6.0, 8.0, 8.0, 8.0]).predict("star")["form"], "hot")
        self.assertEqual(self.rated([8.0, 8.0, 8.0, 8.0, 6.0, 6.0, 6.0]).predict("star")["form"], "cold")
        self.assertEqual(self.rated([7.0, 7.1, 6.9, 7.0, 7.0]).predict("star")["form"], "steady")
        self.assertIsNone(self.rated([7.0, 7.0]).predict("star")["form"])         # too few rated games to call it

    def test_a_stronger_team_edge_lifts_the_rating_within_bounds(self):
        t = self.rated([7.0, 7.0, 7.0], params={"rating_edge_weight": 0.5})
        self.assertGreater(t.predict("star", edge=1.0)["expected_rating"], t.predict("star", edge=0.0)["expected_rating"])
        self.assertLessEqual(t.predict("star", edge=100.0)["expected_rating"], 10.0)
        self.assertGreaterEqual(t.predict("star", edge=-100.0)["expected_rating"], 4.0)


def league(n_matches=40, seed=3):
    """A stream of matches: a few strikers who really score more than the rest."""
    rng = random.Random(seed)
    rates = {"s1": 0.9, "s2": 0.6, **{f"m{i}": 0.12 for i in range(6)}, **{f"d{i}": 0.04 for i in range(4)}}
    positions = {**{p: "Offence" for p in ("s1", "s2")}, **{f"m{i}": "Midfield" for i in range(6)}, **{f"d{i}": "Defence" for i in range(4)}}
    out = []
    for day in range(n_matches):
        lines = {}
        for pid, rate in rates.items():
            minutes = 90 if rng.random() < 0.85 else 0
            g = sum(1 for _ in range(3) if rng.random() < rate / 3) if minutes else 0
            lines[pid] = line(minutes, g=g, xg=rate * 0.9 if minutes else 0.0, rt=6.5 + 0.2 * g + rng.random() * 0.8 if minutes else None)
        out.append({"date": f"2026-{8 + day // 28:02d}-{1 + day % 28:02d}", "teams": {1: lines}})
    return out, positions


class WalkForwardTests(SimpleTestCase):
    def test_it_finds_the_real_scorers_and_beats_the_baselines(self):
        matches, positions = league()
        out = pp.walk_forward(matches, positions, {"model": {}})
        s = pp.summarise(out["model"])
        self.assertGreater(s["goal"]["auc"], 0.65)
        self.assertGreater(s["top3_scorer_hit_rate"], s["top3_random_hit_rate"])
        constant = out["baselines"]["constant_rate"]["goal"]
        self.assertLess(pp.log_loss(out["model"]["goal"]), pp.log_loss(constant))
        self.assertIn("last_rating", out["baselines"])
        self.assertTrue(out["baselines"]["season_mean_rating"]["rating"])

    def test_predictions_for_a_match_never_depend_on_that_match_or_later_ones(self):
        matches, positions = league(30)
        honest = pp.walk_forward(matches, positions, {"m": {}}, record=(10, 12))["m"]
        wrecked = [dict(m, teams={1: {pid: dict(l, g=5, a=5) for pid, l in m["teams"][1].items()}}) if i >= 10 else m
                   for i, m in enumerate(matches)]
        cheat = pp.walk_forward(wrecked, positions, {"m": {}}, record=(10, 11))["m"]
        first = pp.walk_forward(matches, positions, {"m": {}}, record=(10, 11))["m"]
        self.assertEqual([p for p, _y in first["goal"]], [p for p, _y in cheat["goal"]])      # same forecasts, different actuals
        self.assertEqual(len(honest["goal"]) > len(first["goal"]), True)

    def test_the_record_window_limits_what_is_scored(self):
        matches, positions = league(30)
        full = pp.walk_forward(matches, positions, {"m": {}})["m"]["goal"]
        part = pp.walk_forward(matches, positions, {"m": {}}, record=(10, 20))["m"]["goal"]
        self.assertLess(len(part), len(full))

    def test_context_hook_is_used(self):
        matches, positions = league(20)
        calls = []
        pp.walk_forward(matches, positions, {"m": {}}, context=lambda m, team: calls.append(team) or (1.0, 1.0, 0.0))
        self.assertTrue(calls)

    def test_tune_returns_a_valid_parameter_set(self):
        matches, positions = league(24)
        chosen = pp.tune(matches, positions, None, 16, log=lambda *_: None)
        self.assertEqual(set(chosen), set(pp.PARAMS))
        self.assertIn(chosen["xg_weight"], (0.0, 0.5, 0.8, 1.0))


class ScoringHelperTests(SimpleTestCase):
    def test_auc(self):
        self.assertEqual(pp.auc([(0.9, True), (0.1, False)]), 1.0)
        self.assertEqual(pp.auc([(0.1, True), (0.9, False)]), 0.0)
        self.assertEqual(pp.auc([(0.5, True), (0.5, False)]), 0.5)
        self.assertIsNone(pp.auc([(0.5, False)]))

    def test_brier_and_log_loss(self):
        pairs = [(0.8, True), (0.2, False)]
        self.assertAlmostEqual(pp.brier(pairs), 0.04)
        self.assertAlmostEqual(pp.log_loss(pairs), -math.log(0.8))
        self.assertTrue(math.isfinite(pp.log_loss([(0.0, True)])))
