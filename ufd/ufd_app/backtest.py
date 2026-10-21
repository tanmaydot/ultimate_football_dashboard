"""Measuring the match model on history, without leakage.

``walk_forward`` replays seasons in date order. For every match the model is given only the matches played
on earlier dates (ratings are refitted every few days, always on data older than the refit date), so the
numbers below are what the model would have said on the morning of the match.

Scores (lower is better for all but accuracy):

* **RPS**, the ranked probability score: the usual yardstick for home / draw / away forecasts because it
  rewards putting probability near the right outcome on the home-draw-away scale.
* **Log loss** and **Brier**: how much probability went to what happened.
* **Accuracy**: share of matches where the most probable outcome happened (blunt, for orientation only).

The model is compared with a constant forecast (the home / draw / away rates of the previous three years)
and with versions of itself with one ingredient removed. Bookmaker odds are not in any free open dataset
used here, so they are not a benchmark; they are the standard the model would have to beat to be called good.
"""
import math
import random
from bisect import bisect_left
from datetime import date

from .predict_match import DEFAULTS, MatchModel, to_ordinal

OUTCOMES = ("home", "draw", "away")
EPS = 1e-9


def outcome_index(hg, ag):
    return 0 if hg > ag else 1 if hg == ag else 2


def row_rps(row):
    p, o = row["probs"], row["outcome"]
    c1 = p[0] - (o == 0)
    c2 = (p[0] + p[1]) - ((o == 0) + (o == 1))
    return (c1 * c1 + c2 * c2) / 2


def row_brier(row):
    return sum((p - (i == row["outcome"])) ** 2 for i, p in enumerate(row["probs"]))


def row_log_loss(row):
    return -math.log(max(row["probs"][row["outcome"]], EPS))


def row_hit(row):
    return 1.0 if max(range(3), key=lambda i: row["probs"][i]) == row["outcome"] else 0.0


METRICS = {"rps": row_rps, "log_loss": row_log_loss, "brier": row_brier, "accuracy": row_hit}


def score(rows):
    n = len(rows)
    if not n:
        return {"n": 0}
    return {"n": n, **{name: round(sum(fn(r) for r in rows) / n, 4) for name, fn in METRICS.items()}}


def by_season(rows):
    out = {}
    for r in rows:
        d = date.fromisoformat(r["date"])
        out.setdefault(d.year if d.month >= 8 else d.year - 1, []).append(r)
    return {f"{s}-{(s + 1) % 100:02d}": score(v) for s, v in sorted(out.items())}


def calibration(rows, bins=10):
    """Predicted probability vs how often it happened, all three outcomes pooled, in ``bins`` equal bins."""
    cells = [[0, 0.0, 0] for _ in range(bins)]
    for r in rows:
        for i, p in enumerate(r["probs"]):
            b = min(int(p * bins), bins - 1)
            cells[b][0] += 1; cells[b][1] += p; cells[b][2] += (i == r["outcome"])
    return [{"bin": f"{b / bins:.1f}-{(b + 1) / bins:.1f}", "n": n, "predicted": round(sp / n, 3), "observed": round(hit / n, 3)}
            for b, (n, sp, hit) in enumerate(cells) if n]


def paired_bootstrap(rows_a, rows_b, metric=row_rps, resamples=2000, seed=7):
    """Mean of metric(a) - metric(b) over the same matches with a 95% interval (negative = a is better)."""
    diffs = [metric(a) - metric(b) for a, b in zip(rows_a, rows_b)]
    rng, n = random.Random(seed), len(diffs)
    means = sorted(sum(diffs[rng.randrange(n)] for _ in range(n)) / n for _ in range(resamples))
    return {"mean": round(sum(diffs) / n, 5), "low": round(means[int(0.025 * resamples)], 5),
            "high": round(means[int(0.975 * resamples) - 1], 5)}


def walk_forward(model, seasons, variants=None, refit_days=3):
    """Replay ``seasons`` (start years). Returns ``{variant name: [row, ...]}``; ``variants`` maps a name to
    parameter overrides (``{}`` = the model's own parameters)."""
    variants = variants or {"model": {}}
    out = {name: [] for name in variants}
    lo = date(min(seasons), 7, 1).toordinal()
    hi = date(max(seasons) + 1, 7, 1).toordinal()
    fit, fit_day = None, None
    for o, home, away, hg, ag in model.rows[bisect_left(model.ords, lo):bisect_left(model.ords, hi)]:
        if fit is None or o - fit_day >= refit_days:
            fit, fit_day = model.fit(o), o            # uses matches strictly before o
        for name, overrides in variants.items():
            pred = model.predict(home, away, o, fit=fit, params=overrides)
            p = pred["probs"]
            out[name].append({"date": date.fromordinal(o).isoformat(), "home": home, "away": away, "hg": hg, "ag": ag,
                              "outcome": outcome_index(hg, ag), "probs": (p["home"], p["draw"], p["away"]),
                              "xg": (pred["xg"]["home"], pred["xg"]["away"])})
    return out


def climatology(model, rows_like, years=3):
    """Baseline: home / draw / away rates over the three years before each match, the same for every fixture."""
    codes = [outcome_index(r[3], r[4]) for r in model.rows]
    prefix = [[0] * (len(codes) + 1) for _ in range(3)]
    for i, c in enumerate(codes):
        for k in range(3):
            prefix[k][i + 1] = prefix[k][i] + (c == k)
    out = []
    for row in rows_like:
        o = to_ordinal(row["date"])
        hi, lo = bisect_left(model.ords, o), bisect_left(model.ords, o - 365 * years)
        counts = [prefix[k][hi] - prefix[k][lo] for k in range(3)]
        total = sum(counts) or 1
        out.append({**row, "probs": tuple((c or 0.3) / total for c in counts) if sum(counts) else (0.45, 0.25, 0.30)})
    return out


def evaluate(matches, params, test_seasons):
    """The full report on ``test_seasons``: the model, a constant baseline, ablations, significance and calibration."""
    model = MatchModel(matches, params)
    variants = {
        "model": {},
        "with_form_term": {"form_weight": 0.2},
        "with_h2h_term": {"h2h_weight": 0.2, "with_h2h_detail": True},
        "without_dixon_coles": {"rho": 0.0, "draw_boost": 1.0},
    }
    runs = walk_forward(model, test_seasons, variants)
    base = climatology(model, runs["model"])
    flat = MatchModel(matches, {**model.params, "half_life": 1e6})            # every result in the window counts the same
    unshrunk = MatchModel(matches, {**model.params, "shrink": 0.01})
    runs["without_time_decay"] = walk_forward(flat, test_seasons)["model"]
    runs["without_shrinkage"] = walk_forward(unshrunk, test_seasons)["model"]
    report = {
        "test_seasons": [f"{s}-{(s + 1) % 100:02d}" for s in test_seasons],
        "matches": len(base),
        "params": {k: model.params[k] for k in DEFAULTS},
        "scores": {"model": score(runs["model"]), "constant_home_draw_away_rates": score(base),
                   **{k: score(v) for k, v in runs.items() if k != "model"}},
        "rps_difference_vs": {"constant_home_draw_away_rates": paired_bootstrap(runs["model"], base),
                              **{k: paired_bootstrap(runs["model"], v) for k, v in runs.items() if k != "model"}},
        "per_season": by_season(runs["model"]),
        "per_season_constant": by_season(base),
        "calibration": calibration(runs["model"]),
        "goals": goal_error(runs["model"]),
    }
    return report, runs


def goal_error(rows):
    n = len(rows)
    return {"mean_predicted_home_goals": round(sum(r["xg"][0] for r in rows) / n, 3),
            "mean_actual_home_goals": round(sum(r["hg"] for r in rows) / n, 3),
            "mean_predicted_away_goals": round(sum(r["xg"][1] for r in rows) / n, 3),
            "mean_actual_away_goals": round(sum(r["ag"] for r in rows) / n, 3),
            "mae_total_goals": round(sum(abs(r["xg"][0] + r["xg"][1] - r["hg"] - r["ag"]) for r in rows) / n, 3)}


def _mean_rps(matches, params, seasons, variants):
    model = MatchModel(matches, params)
    runs = walk_forward(model, seasons, variants)
    return {name: sum(row_rps(r) for r in rows) / len(rows) for name, rows in runs.items()}


def tune(matches, tune_seasons, log=print):
    """Pick the parameters that minimise mean RPS on ``tune_seasons`` only, one group at a time.

    The held-out seasons are never read here. Options that share the fitted ratings (the weights) are scored in
    one pass; options that change the fit or the number of matches looked at get a pass of their own.
    Returns the chosen parameter dict.
    """
    chosen = dict(DEFAULTS)
    scores = {(hl, k): _mean_rps(matches, {**chosen, "half_life": hl, "shrink": k}, tune_seasons, {"m": {}})["m"]
              for hl in (180.0, 365.0, 730.0) for k in (3.0, 6.0, 12.0)}
    chosen["half_life"], chosen["shrink"] = min(scores, key=scores.get)
    log(f"half_life/shrink -> {chosen['half_life']:.0f}/{chosen['shrink']:.0f}  (RPS {min(scores.values()):.4f})")

    for label, count_key, counts, weight_key, weights in (
            ("form", "form_matches", (4, 6, 10), "form_weight", (0.0, 0.25, 0.5, 0.75, 1.0)),
            ("head to head", "h2h_matches", (4, 6, 10), "h2h_weight", (0.0, 0.25, 0.5, 1.0))):
        results = {}
        for count in counts:
            variants = {w: {weight_key: w, "with_h2h_detail": True} for w in weights}
            for w, rps in _mean_rps(matches, {**chosen, count_key: count}, tune_seasons, variants).items():
                results[(count, w)] = rps
        (count, weight) = min(results, key=lambda k: (results[k], k[1]))
        chosen[count_key], chosen[weight_key] = count, weight
        log(f"{label} -> {count_key}={count}, {weight_key}={weight}  (RPS {results[(count, weight)]:.4f}; weight 0 gives {min(v for (c, w), v in results.items() if w == 0):.4f})")

    options = {(r, d): {"rho": r, "draw_boost": d} for r in (0.0, -0.05, -0.1, -0.15) for d in (1.0, 1.05, 1.1, 1.15)}
    results = _mean_rps(matches, chosen, tune_seasons, options)
    best = min(results, key=lambda k: (results[k], abs(k[0]) + abs(k[1] - 1)))
    chosen["rho"], chosen["draw_boost"] = best
    log(f"dixon-coles / draws -> rho={best[0]}, draw_boost={best[1]}  (RPS {results[best]:.4f}; neither gives {results[(0.0, 1.0)]:.4f})")
    return chosen
