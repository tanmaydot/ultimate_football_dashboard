"""What a player is likely to do in his club's next match.

Source: the per-match player lines PitchAPI gives for every finished match (minutes, goals, assists, shots,
xG, xA, rating), kept in ``pl_history.json``. They only exist for the current season, so everything here is
learned from a small sample and shrunk hard towards position averages.

For each player the model produces (all for the *next* match):

* **Expected minutes**: a recency weighted average over the club's last matches (a match he was not in the
  squad for counts as 0 minutes), and the share of those matches he played in.
* **Goal and assist rates per 90** blended from three signals, each shrunk towards the average of his
  position with ``k90`` pseudo-matches so a player with one 90 and one goal is not rated a 1.0 goals/90 striker:
  the whole season, the last ``recent_matches`` appearances (the "most recent match" signal, ``recent_weight``)
  and expected goals / assists (``xg_weight``; xG is a steadier measure of chances than goals, rescaled so
  it matches the real goal total).
* **Opponent and venue.** The rate is multiplied by the opponent's defence rating (from the team model) and
  the home/away goal difference, raised to ``context_exponent``.
* **Probabilities.** ``P(goal) = 1 - exp(-rate/90 * expected_minutes * context)``, the same for assists, and
  for "a goal or an assist".
* **Expected rating**: recency weighted mean of his match ratings shrunk towards the position average, moved by
  how much better or worse his team is expected to be than the opponent (``rating_edge_weight``), and a
  trend label (hot / steady / cold) from how far his last ratings are from his season mean.

``PlayerTracker`` keeps the running totals; the same object produces live predictions and, replayed match
by match in ``walk_forward``, the backtest, so the two cannot drift apart. Predictions made for a match only
use matches dated before it.
"""
import math
from collections import defaultdict

# Chosen on the first 40 matches of the season only (``tune``); the later matches were scored afterwards as a
# hold-out (explain.md). recent_weight is 0.15 rather than the tuned 0: validation cannot tell 0 from 0.3 apart,
# so the most recent matches are kept in the blend at a small weight.
PARAMS = {
    "k90": 2.0,             # pseudo-90s of position average in a season rate (goals / assists)
    "k90_xg": 3.0,          # same for xG / xA rates
    "recent_matches": 5,    # appearances in the "recent" signal
    "recent_half_life": 2.5,  # appearances until a match counts half as much
    "k90_recent": 6.0,      # pseudo-90s in the recent rate
    "recent_weight": 0.15,  # share of the recent rate in the blend (0 = season only)
    "xg_weight": 0.8,       # share of the xG / xA rate in the blend (0 = goals / assists only)
    "minutes_matches": 8,   # club matches in the minutes average
    "minutes_half_life": 3.0,
    "context_exponent": 1.0,  # strength of the opponent / venue adjustment (0 = none)
    "k_rating": 4.0,        # pseudo-appearances of position mean in the expected rating
    "rating_half_life": 2.0,
    "rating_edge_weight": 0.3,  # rating points per expected-goal advantage of his team in the match
    "trend_margin": 0.35,   # rating points above / below the season mean that count as hot / cold
}
GROUPS = ("Goalkeeper", "Defence", "Midfield", "Offence")
UNKNOWN = "Unknown"
MIN_GOALS_TO_RESCALE = 10   # xG is rescaled to the real goal total only once there are this many goals
MIN_FOR_RATING = 15          # a rating from fewer minutes is mostly noise


def clamp(x, low, high):
    return max(low, min(high, x))


class PlayerState:
    __slots__ = ("team", "group", "games")

    def __init__(self, team, group):
        self.team, self.group = team, group
        self.games = []     # [(minutes, goals, assists, xg, xa, rating|None, shots)] one per club match since first seen


class PlayerTracker:
    """Running per-player state. ``observe`` feeds finished matches in date order; ``predict`` reads the state."""

    def __init__(self, positions=None, params=None):
        self.params = {**PARAMS, **(params or {})}
        self.positions = positions or {}        # player id -> position group
        self.players = {}                       # player id -> PlayerState
        self.team_matches = defaultdict(int)
        self.total = defaultdict(lambda: {"min": 0.0, "g": 0.0, "a": 0.0, "xg": 0.0, "xa": 0.0, "r": 0.0, "rn": 0})
        self.matches_seen = 0

    def group_of(self, pid):
        return self.positions.get(pid) or UNKNOWN

    # ---- learning ---------------------------------------------------------------
    def observe(self, match):
        """``match``: {"teams": {team id: {player id: line}}} where a line has min, g, a, xg, xa, rt, sh (any may be missing)."""
        for team, lines in match["teams"].items():
            for pid, line in lines.items():
                state = self.players.get(pid)
                if state is None:
                    state = self.players[pid] = PlayerState(team, self.group_of(pid))
                state.team = team
            for pid, state in self.players.items():
                if state.team != team:
                    continue
                line = lines.get(pid)
                minutes = (line or {}).get("min") or 0
                rating = (line or {}).get("rt") if minutes >= MIN_FOR_RATING else None
                game = (minutes, (line or {}).get("g", 0), (line or {}).get("a", 0), (line or {}).get("xg", 0.0),
                        (line or {}).get("xa", 0.0), rating, (line or {}).get("sh", 0))
                state.games.append(game)
                t = self.total[state.group]
                t["min"] += minutes; t["g"] += game[1]; t["a"] += game[2]; t["xg"] += game[3]; t["xa"] += game[4]
                if rating is not None:
                    t["r"] += rating; t["rn"] += 1
            self.team_matches[team] += 1
        self.matches_seen += 1

    # ---- priors -----------------------------------------------------------------------
    def _prior(self, group, key):
        """Per-90 rate of a position group, falling back to everybody when the group has little data."""
        t = self.total[group]
        if t["min"] < 900:
            t = {k: sum(self.total[g][k] for g in list(self.total)) for k in t}
        return 90 * t[key] / t["min"] if t["min"] else 0.0

    def _xg_scale(self, goal_key, xg_key):
        goals = sum(t[goal_key] for t in self.total.values())
        xg = sum(t[xg_key] for t in self.total.values())
        return goals / xg if xg > 0 and goals >= MIN_GOALS_TO_RESCALE else 1.0   # too few goals to trust the ratio yet

    def _rating_prior(self, group):
        t = self.total[group]
        if t["rn"] < 30:
            t = {"r": sum(x["r"] for x in self.total.values()), "rn": sum(x["rn"] for x in self.total.values())}
        return t["r"] / t["rn"] if t["rn"] else 6.8

    # ---- one player -----------------------------------------------------------------------
    def _rate(self, state, goal_i, xg_i, key, xg_key):
        """Blended goals (or assists) per 90 for one player."""
        p = self.params
        prior = self._prior(state.group, key)
        prior_xg = self._prior(state.group, xg_key) * self._xg_scale(key, xg_key)
        played = [g for g in state.games if g[0] > 0]
        minutes = sum(g[0] for g in played)
        season_goals = (sum(g[goal_i] for g in played) + p["k90"] * prior) / (minutes / 90 + p["k90"])
        season_xg = (sum(g[xg_i] for g in played) * self._xg_scale(key, xg_key) + p["k90_xg"] * prior_xg) / (minutes / 90 + p["k90_xg"])
        recent = played[-int(p["recent_matches"]):][::-1]
        weights = [0.5 ** (i / p["recent_half_life"]) for i in range(len(recent))]
        r_minutes = sum(w * g[0] / 90 for w, g in zip(weights, recent))
        recent_goals = (sum(w * g[goal_i] for w, g in zip(weights, recent)) + p["k90_recent"] * prior) / (r_minutes + p["k90_recent"])
        recent_xg = (sum(w * g[xg_i] for w, g in zip(weights, recent)) * self._xg_scale(key, xg_key) + p["k90_recent"] * prior_xg) / (r_minutes + p["k90_recent"])
        goals_est = (1 - p["recent_weight"]) * season_goals + p["recent_weight"] * recent_goals
        xg_est = (1 - p["recent_weight"]) * season_xg + p["recent_weight"] * recent_xg
        return (1 - p["xg_weight"]) * goals_est + p["xg_weight"] * xg_est

    def expected_minutes(self, state):
        p = self.params
        recent = state.games[-int(p["minutes_matches"]):][::-1]
        if not recent:
            return 0.0, 0.0
        weights = [0.5 ** (i / p["minutes_half_life"]) for i in range(len(recent))]
        total = sum(weights)
        return (sum(w * g[0] for w, g in zip(weights, recent)) / total,
                sum(w * (g[0] > 0) for w, g in zip(weights, recent)) / total)

    def expected_rating(self, state):
        p = self.params
        rated = [g[5] for g in state.games if g[5] is not None][::-1]
        prior = self._rating_prior(state.group)
        if not rated:
            return None, None, 0
        weights = [0.5 ** (i / p["rating_half_life"]) for i in range(len(rated))]
        mean = (sum(w * r for w, r in zip(weights, rated)) + p["k_rating"] * prior) / (sum(weights) + p["k_rating"])
        season = sum(rated) / len(rated)
        trend = (sum(weights[:3]) and sum(w * r for w, r in zip(weights[:3], rated[:3])) / sum(weights[:3]) - season) if len(rated) >= 4 else None
        return mean, trend, len(rated)

    def predict(self, pid, opponent_defence=1.0, venue_factor=1.0, edge=0.0):
        """The next-match prediction for one player (None for a player the tracker has never seen)."""
        state = self.players.get(pid)
        p = self.params
        if state is None:
            return None
        minutes, p_play = self.expected_minutes(state)
        context = (opponent_defence * venue_factor) ** p["context_exponent"]
        goals_rate = self._rate(state, 1, 3, "g", "xg")
        assists_rate = self._rate(state, 2, 4, "a", "xa")
        lam_g = goals_rate / 90 * minutes * context
        lam_a = assists_rate / 90 * minutes * context
        rating, trend, rated_n = self.expected_rating(state)
        if rating is not None:
            rating = clamp(rating + p["rating_edge_weight"] * edge, 4.0, 10.0)
        played = [g for g in state.games if g[0] > 0]
        label = None
        if trend is not None:
            label = "hot" if trend >= p["trend_margin"] else "cold" if trend <= -p["trend_margin"] else "steady"
        return {
            "group": state.group,
            "expected_minutes": minutes,
            "p_play": p_play,
            "goals_per_90": goals_rate, "assists_per_90": assists_rate,
            "expected_goals": lam_g, "expected_assists": lam_a,
            "p_goal": 1 - math.exp(-lam_g), "p_assist": 1 - math.exp(-lam_a),
            "p_goal_or_assist": 1 - math.exp(-(lam_g + lam_a)),
            "expected_rating": rating, "rating_trend": trend, "form": label,
            "appearances": len(played), "minutes_played": sum(g[0] for g in played), "rated_appearances": rated_n,
            "confidence": "high" if sum(g[0] for g in played) >= 900 else "medium" if sum(g[0] for g in played) >= 270 else "low",
        }

    def last_game(self, pid):
        state = self.players.get(pid)
        played = [g for g in (state.games if state else []) if g[0] > 0]
        return played[-1] if played else None


# --------------------------------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------------------------------
EPS = 1e-6


def brier(pairs):
    return sum((p - y) ** 2 for p, y in pairs) / len(pairs)


def log_loss(pairs):
    return -sum(math.log(clamp(p if y else 1 - p, EPS, 1)) for p, y in pairs) / len(pairs)


def auc(pairs):
    """Chance that a scorer got a higher probability than a non-scorer (0.5 = no skill)."""
    pos = sorted(p for p, y in pairs if y)
    neg = sorted(p for p, y in pairs if not y)
    if not pos or not neg:
        return None
    wins = 0.0
    j = 0
    for p in pos:
        while j < len(neg) and neg[j] < p:
            j += 1
        ties = sum(1 for x in neg[j:j + 50] if x == p)
        wins += j + ties / 2
    return wins / (len(pos) * len(neg))


def _empty_result():
    return {"goal": [], "assist": [], "either": [], "rating": [], "top3_hits": 0, "top3_teams": 0, "top3_random": 0.0}


def rounded_auc(pairs):
    """``auc`` rounded for reports; None when it is undefined (nobody, or everybody, scored)."""
    value = auc(pairs)
    return None if value is None else round(value, 3)


def walk_forward(matches, positions, variants, context=None, warmup=3, record=None):
    """Replay ``matches`` (oldest first). Before each match every variant predicts for every player it already knows
    for each of the two clubs; then the match is learned. ``context(match, team) -> (opponent_defence, venue, edge)``.
    ``record=(first, last)`` keeps only the predictions for matches ``first <= number < last`` (the rest still teach).

    Returns {variant: results} plus ``"baselines"``: simple forecasts scored on exactly the same player-matches.
    A results dict has goal / assist / either: [(probability, happened)], rating: [(predicted, actual)] and top-3 counts.
    """
    first, last = record or (warmup, len(matches))
    first = max(first, warmup)
    trackers = {name: PlayerTracker(positions, params) for name, params in variants.items()}
    lead = next(iter(trackers.values()))
    out = {name: _empty_result() for name in variants}
    base = {name: {"goal": [], "rating": []} for name in ("constant_rate", "season_goals_per_match", "position_average",
                                                           "last_rating", "season_mean_rating", "position_mean_rating")}
    seen = {"goals": 0.0, "pairs": 0}
    for number, match in enumerate(matches):
        if first <= number < last:
            for team, lines in match["teams"].items():
                opp_def, venue, edge = context(match, team) if context else (1.0, 1.0, 0.0)
                for name, tracker in trackers.items():
                    result = out[name]
                    scored = []
                    for pid, state in tracker.players.items():
                        if state.team != team:
                            continue
                        pred = tracker.predict(pid, opp_def, venue, edge)
                        line = lines.get(pid) or {}
                        goals, assists = line.get("g", 0), line.get("a", 0)
                        result["goal"].append((pred["p_goal"], goals > 0))
                        result["assist"].append((pred["p_assist"], assists > 0))
                        result["either"].append((pred["p_goal_or_assist"], goals + assists > 0))
                        rated = (line.get("min") or 0) >= MIN_FOR_RATING and line.get("rt") is not None
                        if rated and pred["expected_rating"] is not None:
                            result["rating"].append((pred["expected_rating"], line["rt"]))
                        scored.append((pred["p_goal"], goals > 0))
                        if tracker is lead:
                            n_club = max(len(state.games), 1)
                            base["constant_rate"]["goal"].append((seen["goals"] / seen["pairs"] if seen["pairs"] else 0.05, goals > 0))
                            base["season_goals_per_match"]["goal"].append(
                                (clamp(sum(g[1] for g in state.games) / n_club, 0.005, 0.9), goals > 0))
                            minutes = tracker.expected_minutes(state)[0]
                            base["position_average"]["goal"].append(
                                (1 - math.exp(-tracker._prior(state.group, "g") / 90 * minutes), goals > 0))
                            if rated and pred["expected_rating"] is not None:       # the same player-matches the model is scored on
                                ratings = [g[5] for g in state.games if g[5] is not None]
                                base["last_rating"]["rating"].append((ratings[-1], line["rt"]))
                                base["season_mean_rating"]["rating"].append((sum(ratings) / len(ratings), line["rt"]))
                                base["position_mean_rating"]["rating"].append((tracker._rating_prior(state.group), line["rt"]))
                            seen["goals"] += goals > 0
                            seen["pairs"] += 1
                    if scored and any(y for _p, y in scored):
                        top = sorted(scored, key=lambda t: -t[0])[:3]
                        n, scorers = len(scored), sum(1 for _p, y in scored if y)
                        result["top3_teams"] += 1
                        result["top3_hits"] += any(y for _p, y in top)
                        result["top3_random"] += 1 - (math.comb(n - scorers, min(3, n)) / math.comb(n, min(3, n)) if n >= 3 else 0.0)
        for tracker in trackers.values():
            tracker.observe(match)
    out["baselines"] = base
    return out


def tune(matches, positions, context, split, log=print):
    """Choose the player parameters on matches before ``split`` only (log loss on goals and assists; MAE for ratings)."""
    def goal_assist_loss(params):
        run = walk_forward(matches, positions, {"m": params}, context, record=(3, split))["m"]
        return (log_loss(run["goal"]) + log_loss(run["assist"])) / 2

    chosen = dict(PARAMS)
    grid = [{"k90": k, "recent_weight": r, "xg_weight": x, "context_exponent": c}
            for k in (2.0, 4.0, 8.0) for r in (0.0, 0.3, 0.6) for x in (0.0, 0.5, 0.8, 1.0) for c in (0.0, 0.5, 1.0)]
    scored = {i: goal_assist_loss({**chosen, **g}) for i, g in enumerate(grid)}
    best = min(scored, key=scored.get)
    chosen.update(grid[best])
    log(f"goals/assists -> {grid[best]}  (log loss {scored[best]:.4f}; all-defaults {goal_assist_loss(chosen if False else PARAMS):.4f})")

    def rating_mae(params):
        run = walk_forward(matches, positions, {"m": params}, context, record=(3, split))["m"]["rating"]
        return sum(abs(p - a) for p, a in run) / len(run)

    options = [{"k_rating": k, "rating_half_life": h, "rating_edge_weight": e}
               for k in (1.0, 2.0, 4.0) for h in (2.0, 3.0, 6.0) for e in (0.0, 0.15, 0.3, 0.5)]
    maes = {i: rating_mae({**chosen, **o}) for i, o in enumerate(options)}
    best = min(maes, key=maes.get)
    chosen.update(options[best])
    log(f"rating -> {options[best]}  (MAE {maes[best]:.3f})")
    return chosen


def summarise(result):
    """Scores of one variant from ``walk_forward``."""
    goal = result["goal"]
    rating = result["rating"]
    return {
        "player_matches": len(goal),
        "goal_rate_actual": round(sum(y for _p, y in goal) / len(goal), 4),
        "goal": {"brier": round(brier(goal), 5), "log_loss": round(log_loss(goal), 4), "auc": rounded_auc(goal)},
        "assist": {"brier": round(brier(result["assist"]), 5), "log_loss": round(log_loss(result["assist"]), 4),
                   "auc": rounded_auc(result["assist"])},
        "goal_or_assist": {"brier": round(brier(result["either"]), 5), "log_loss": round(log_loss(result["either"]), 4),
                           "auc": rounded_auc(result["either"])},
        "top3_scorer_hit_rate": round(result["top3_hits"] / result["top3_teams"], 3) if result["top3_teams"] else None,
        "top3_random_hit_rate": round(result["top3_random"] / result["top3_teams"], 3) if result["top3_teams"] else None,
        "rating": {"n": len(rating), "mae": round(sum(abs(p - a) for p, a in rating) / len(rating), 3) if rating else None},
    }
