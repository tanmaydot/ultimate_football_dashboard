"""Runs both models on history and writes ``data/pl_validation.json``.

* Match model: walk-forward over 2016-17 to today. Its parameters were tuned on 2005-06 to 2015-16 only.
* Player model: walk-forward over the matches in ``pl_history.json``. Parameters were tuned on the first
  ``split`` matches; the later matches are the hold-out. The sample is one partial season, so read the
  player numbers as indicative: there are only a few dozen scorers in the hold-out.
"""
from . import backtest
from . import predict_player as pp
from .openfootball import current_season_start
from .predict_match import DEFAULTS
from .predictions import make_context, merge_current_season, player_inputs, team_directory
from .predict_match import MatchModel
from .util import utc_now_iso

TUNE_SEASONS = list(range(2005, 2016))
TEST_FIRST_SEASON = 2016
PLAYER_SPLIT_SHARE = 0.58      # first 58% of the matches tune, the rest is the hold-out


def team_validation(played, params=None, today=None, tune=False, log=print):
    """The match-model report (see ``backtest.evaluate``). ``tune=True`` re-derives the parameters first."""
    if tune:
        params = backtest.tune(played, TUNE_SEASONS, log=log)
    report, _runs = backtest.evaluate(played, params or DEFAULTS, list(range(TEST_FIRST_SEASON, current_season_start(today) + 1)))
    report["tuned_on_seasons"] = [f"{TUNE_SEASONS[0]}-{(TUNE_SEASONS[0] + 1) % 100:02d}", f"{TUNE_SEASONS[-1]}-{(TUNE_SEASONS[-1] + 1) % 100:02d}"]
    return report


def _window(out, variants):
    summaries = {name: pp.summarise(out[name]) for name in variants}
    rows = {}
    for name, run in out["baselines"].items():
        if run["goal"]:
            rows[name] = {"brier": round(pp.brier(run["goal"]), 5), "log_loss": round(pp.log_loss(run["goal"]), 4),
                          "auc": pp.rounded_auc(run["goal"])}
        if run["rating"]:
            rows[name] = {"mae": round(sum(abs(p - a) for p, a in run["rating"]) / len(run["rating"]), 3), "n": len(run["rating"])}
    scorers = sum(1 for _p, y in out[next(iter(variants))]["goal"] if y)
    return {"variants": summaries, "baselines": rows, "scorers": scorers}


def player_validation(pl, history, played, player_params=None, log=print):
    by_id, _by_key = team_directory(pl)
    model = MatchModel(played, DEFAULTS)
    matches, positions, _ = player_inputs(pl, history, by_id)
    if len(matches) < 12:
        return None
    context = make_context(model, by_id)
    split = int(len(matches) * PLAYER_SPLIT_SHARE)
    params = {**pp.PARAMS, **(player_params or {})}
    variants = {"model": params, "season_totals_only": {**params, "recent_weight": 0.0, "xg_weight": 0.0},
                "without_xg": {**params, "xg_weight": 0.0}, "without_recent": {**params, "recent_weight": 0.0},
                "without_opponent_context": {**params, "context_exponent": 0.0, "rating_edge_weight": 0.0}}
    result = {"matches": len(matches), "split_after_match": split, "params": params}
    for label, window in (("tuning_window", (3, split)), ("holdout_window", (split, len(matches)))):
        out = pp.walk_forward(matches, positions, variants, context, record=window)
        result[label] = {"matches": window[1] - window[0], **_window(out, variants)}
    return result


def run(pl, history, archive, params=None, player_params=None, today=None, tune=False, log=print):
    by_id, _by_key = team_directory(pl)
    played, _fixtures, _added = merge_current_season(archive, (history or {}).get("results"),
                                                     {tid: info["key"] for tid, info in by_id.items()}, today)
    return {
        "generated_at": utc_now_iso(),
        "team_model": team_validation(played, params, today, tune, log),
        "player_model": player_validation(pl, history or {}, played, player_params, log),
        "notes": ["Predictions use only matches dated before each match; ratings are refitted every 3 days on older data.",
                  "Bookmaker odds are not part of any free open dataset used here, so there is no market benchmark.",
                  "Player lines exist only for the current season: the player scores come from a small sample."],
    }
