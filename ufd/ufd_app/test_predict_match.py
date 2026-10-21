"""The match model: probabilities, leakage, edge cases and the ingredients one by one."""
import math
import random
from datetime import date, timedelta

from django.test import SimpleTestCase

from . import predict_match as pm
from .predict_match import MatchModel, outcome_probabilities, poisson_pmf, scoreline_grid, scoreline_summary


def poisson(rng, lam):
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def synthetic(seasons=4, teams=8, seed=1, strengths=None, first=2018):
    """Double round robin per season among clubs with known strengths; goals drawn from the true Poisson rates."""
    rng = random.Random(seed)
    names = [f"t{i}" for i in range(teams)]
    strengths = strengths or {n: (1.5 - 0.15 * i, 0.7 + 0.1 * i) for i, n in enumerate(names)}   # (attack, defence)
    out = []
    for s in range(seasons):
        day = date(first + s, 8, 10)
        pairs = [(h, a) for h in names for a in names if h != a]
        rng.shuffle(pairs)
        for i, (h, a) in enumerate(pairs):
            lam_h = 1.45 * strengths[h][0] * strengths[a][1]
            lam_a = 1.15 * strengths[a][0] * strengths[h][1]
            out.append({"date": (day + timedelta(days=3 * (i // 4))).isoformat(), "home": h, "away": a,
                        "hg": poisson(rng, lam_h), "ag": poisson(rng, lam_a)})
    return out


class MathTests(SimpleTestCase):
    def test_poisson_pmf_sums_to_one(self):
        self.assertAlmostEqual(sum(poisson_pmf(1.7, 40)), 1.0, places=9)

    def test_scoreline_grid_is_a_distribution(self):
        for rho in (0.0, -0.1, 0.1):
            grid = scoreline_grid(1.6, 1.1, rho, 9)
            self.assertAlmostEqual(sum(map(sum, grid)), 1.0, places=9)
            self.assertTrue(all(p >= 0 for row in grid for p in row))

    def test_equal_goal_expectations_give_equal_home_and_away(self):
        home, draw, away = outcome_probabilities(scoreline_grid(1.3, 1.3))
        self.assertAlmostEqual(home, away, places=9)
        self.assertAlmostEqual(home + draw + away, 1.0, places=9)

    def test_dixon_coles_moves_draw_probability(self):
        base = outcome_probabilities(scoreline_grid(1.2, 1.0, 0.0))[1]
        more = outcome_probabilities(scoreline_grid(1.2, 1.0, -0.15))[1]
        self.assertGreater(more, base)

    def test_draw_boost_renormalises(self):
        grid = scoreline_grid(1.4, 1.1)
        plain, boosted = outcome_probabilities(grid), outcome_probabilities(grid, 1.2)
        self.assertGreater(boosted[1], plain[1])
        self.assertAlmostEqual(sum(boosted), 1.0, places=9)

    def test_scoreline_summary_markets(self):
        summary = scoreline_summary(scoreline_grid(3.0, 0.3))
        self.assertGreater(summary["over_2_5"], 0.5)
        self.assertGreater(summary["clean_sheet"]["home"], 0.6)
        self.assertEqual(len(summary["top_scores"]), 3)
        self.assertGreaterEqual(summary["top_scores"][0]["p"], summary["top_scores"][1]["p"])
        low = scoreline_summary(scoreline_grid(0.4, 0.3))
        self.assertLess(low["over_2_5"], 0.1)


class PredictionTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.matches = synthetic()
        cls.model = MatchModel(cls.matches)
        cls.day = date(2021, 8, 1)

    def test_probabilities_are_valid(self):
        pred = self.model.predict("t0", "t7", self.day)
        self.assertAlmostEqual(sum(pred["probs"].values()), 1.0, places=9)
        self.assertTrue(all(0 < p < 1 for p in pred["probs"].values()))
        self.assertAlmostEqual(sum(map(sum, pred["grid"])), 1.0, places=9)

    def test_the_stronger_side_is_favoured_and_the_order_matters(self):
        strong_home = self.model.predict("t0", "t7", self.day)["probs"]
        strong_away = self.model.predict("t7", "t0", self.day)["probs"]
        self.assertGreater(strong_home["home"], 0.6)
        self.assertGreater(strong_away["away"], strong_away["home"])
        self.assertGreater(strong_home["home"], strong_away["away"])             # home advantage

    def test_home_advantage_is_measured_not_assumed(self):
        pred = self.model.predict("t3", "t4", self.day)                          # near-equal sides
        self.assertGreater(pred["xg"]["home"], pred["xg"]["away"] * 0.95)
        fit = self.model.fit(self.day)
        self.assertGreater(fit.mu_home, fit.mu_away)

    def test_no_history_still_gives_valid_probabilities(self):
        pred = MatchModel([]).predict("a", "b", "2026-08-15")
        self.assertAlmostEqual(sum(pred["probs"].values()), 1.0, places=9)
        self.assertGreater(pred["probs"]["home"], pred["probs"]["away"])         # the default home edge
        self.assertEqual(MatchModel([]).fit("2026-08-15").matches_used, 0)

    def test_unknown_clubs_get_average_ratings(self):
        pred = self.model.predict("never-seen-1", "never-seen-2", self.day)
        self.assertEqual(pred["ratings"]["home"], (1.0, 1.0))
        self.assertAlmostEqual(sum(pred["probs"].values()), 1.0, places=9)

    def test_unplayed_matches_in_the_input_are_ignored(self):
        extra = self.matches + [{"date": "2022-01-01", "home": "t0", "away": "t1", "hg": None, "ag": None}]
        self.assertEqual(len(MatchModel(extra).rows), len(self.matches))

    def test_expectations_are_clipped(self):
        wild = [{"date": "2020-09-01", "home": "a", "away": "b", "hg": 15, "ag": 0}] * 30 + synthetic(1, 4, 3)
        pred = MatchModel(wild).predict("a", "b", "2021-06-01")
        self.assertLessEqual(pred["xg"]["home"], pm.LAMBDA_MAX)
        self.assertGreaterEqual(pred["xg"]["away"], pm.LAMBDA_MIN)


class LeakageTests(SimpleTestCase):
    """The model must not see the match it predicts, or anything after it."""

    def setUp(self):
        self.matches = synthetic(seasons=3, teams=8, seed=5)

    def test_changing_results_on_or_after_the_date_changes_nothing(self):
        cutoff = date(2019, 12, 1)
        tampered = [dict(m) for m in self.matches]
        for m in tampered:
            if m["date"] >= cutoff.isoformat():
                m["hg"], m["ag"] = 9, 0
        honest = MatchModel(self.matches).predict("t1", "t2", cutoff)
        cheat = MatchModel(tampered).predict("t1", "t2", cutoff)
        self.assertEqual(honest["probs"], cheat["probs"])
        self.assertEqual(honest["xg"], cheat["xg"])

    def test_results_before_the_date_do_change_it(self):
        cutoff = date(2019, 12, 1)
        tampered = [dict(m, hg=0, ag=5) if m["date"] < cutoff.isoformat() and m["home"] == "t1" else m for m in self.matches]
        self.assertNotEqual(MatchModel(self.matches).predict("t1", "t2", cutoff)["probs"],
                            MatchModel(tampered).predict("t1", "t2", cutoff)["probs"])

    def test_a_match_played_the_same_day_is_not_used(self):
        day = date(2019, 12, 1)
        base = [m for m in self.matches if m["date"] < day.isoformat()]
        same_day = base + [{"date": day.isoformat(), "home": "t1", "away": "t2", "hg": 7, "ag": 0}]
        self.assertEqual(MatchModel(base).predict("t1", "t2", day)["probs"], MatchModel(same_day).predict("t1", "t2", day)["probs"])

    def test_form_and_head_to_head_use_only_earlier_matches(self):
        day = date(2019, 12, 1)
        params = {"form_weight": 0.5, "h2h_weight": 0.5}
        base = [m for m in self.matches if m["date"] < day.isoformat()]
        future = base + [dict(m, hg=8, ag=0) for m in self.matches if m["date"] >= day.isoformat()]
        self.assertEqual(MatchModel(base, params).predict("t1", "t2", day)["probs"],
                         MatchModel(future, params).predict("t1", "t2", day)["probs"])


class IngredientTests(SimpleTestCase):
    def test_older_results_count_less(self):
        old_good = [{"date": "2018-09-01", "home": "a", "away": "b", "hg": 4, "ag": 0}] * 10
        recent_filler = synthetic(1, 4, 9, first=2020)
        late = MatchModel(old_good + recent_filler, {"half_life": 90.0})
        flat = MatchModel(old_good + recent_filler, {"half_life": 1e6})
        day = date(2021, 6, 1)
        self.assertGreater(flat.predict("a", "b", day)["probs"]["home"], late.predict("a", "b", day)["probs"]["home"])

    def test_newcomers_start_below_average_from_measured_history(self):
        # three seasons where promoted clubs are clearly weak, then a new promoted club with no matches yet
        rng = random.Random(4)
        out = []
        for year in (2017, 2018, 2019):
            old = [f"o{i}" for i in range(11)]       # 22 matches each, above the 20-match minimum
            new = [f"n{year}"]
            for h in old + new:
                for a in old + new:
                    if h != a:
                        weak = lambda t: t.startswith("n")
                        out.append({"date": f"{year}-{9 + rng.randrange(3):02d}-{1 + rng.randrange(25):02d}", "home": h, "away": a,
                                    "hg": poisson(rng, 0.6 if weak(h) else 1.5), "ag": poisson(rng, 2.0 if weak(h) else 1.1)})
        # the old clubs must have played the season before for "promoted" to mean something
        model = MatchModel(out)
        attack, defence = model.newcomer_prior(date(2020, 8, 10).toordinal())
        self.assertLess(attack, 0.9)
        self.assertGreater(defence, 1.1)
        self.assertTrue(model.is_newcomer("n-brand-new", date(2020, 8, 10).toordinal()))
        self.assertFalse(model.is_newcomer("o1", date(2020, 8, 10).toordinal()))
        self.assertEqual(model.fit(date(2020, 8, 10)).rating("n-brand-new", True), (attack, defence))

    def test_form_term_changes_the_forecast_only_when_switched_on(self):
        # t3 has just lost five in a row heavily
        matches = synthetic(3, 8, 11)
        last = date(2020, 12, 1)
        slump = [{"date": (last + timedelta(days=i * 3)).isoformat(), "home": "t3", "away": f"t{4 + i % 3}", "hg": 0, "ag": 4}
                 for i in range(5)]
        data = matches + slump
        day = date(2021, 1, 20)
        off = MatchModel(data, {"form_weight": 0.0}).predict("t3", "t4", day)
        on = MatchModel(data, {"form_weight": 0.8}).predict("t3", "t4", day)
        self.assertLess(on["xg"]["home"], off["xg"]["home"])
        self.assertAlmostEqual(MatchModel(data).predict("t3", "t4", day)["xg"]["home"], off["xg"]["home"], places=9)  # off by default

    def test_head_to_head_summary_and_term(self):
        matches = synthetic(3, 8, 12)
        day = date(2021, 6, 1)
        model = MatchModel(matches, {"h2h_weight": 0.0})
        h2h = model.predict("t1", "t2", day, params={"with_h2h_detail": True})["h2h"]
        self.assertGreaterEqual(h2h["n"], 3)
        record = h2h["record"]
        self.assertEqual(sum(record.values()), h2h["n"])
        self.assertEqual(len(h2h["meetings"]), h2h["n"])
        self.assertGreaterEqual(h2h["meetings"][0]["date"], h2h["meetings"][-1]["date"])    # newest first
        dominated = matches + [{"date": f"2021-0{m}-15", "home": h, "away": a, "hg": 5, "ag": 0}
                               for m, (h, a) in enumerate([("t5", "t6"), ("t6", "t5"), ("t5", "t6")], 1)]
        base = MatchModel(dominated).predict("t6", "t5", date(2021, 6, 1))["probs"]["home"]
        on = MatchModel(dominated, {"h2h_weight": 1.0}).predict("t6", "t5", date(2021, 6, 1))["probs"]["home"]
        self.assertNotEqual(base, on)

    def test_recent_lists_newest_first_and_stops_at_the_date(self):
        model = MatchModel(synthetic(2, 6, 2))
        rows = model.recent("t0", date(2019, 6, 1), 4)
        self.assertEqual(len(rows), 4)
        self.assertEqual([r[0] for r in rows], sorted((r[0] for r in rows), reverse=True))
        self.assertTrue(all(r[0] < "2019-06-01" for r in rows))
        self.assertEqual(model.recent("unknown", date(2019, 6, 1)), [])
