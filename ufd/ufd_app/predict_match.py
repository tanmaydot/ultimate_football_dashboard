"""Win / draw / loss probabilities for a Premier League match.

A goals model, not a classifier: it estimates how many goals each side is expected to score and turns
that into outcome probabilities with a Poisson scoreline grid. Every number comes from matches played
*before* the prediction date, so the same code can be run on history (``backtest``) without leakage.

Steps (the numbers in ``DEFAULTS`` were chosen on 2008-09 to 2015-16 only, see ``backtest.tune``):

The deployed form and head-to-head weights are 0: on 2005-06 to 2015-16 every weight above 0 made the
probabilities worse (the time-decayed ratings already carry the recent-results signal). Both are still computed and
shown as match insights, and can be switched on with the ``form_weight`` / ``h2h_weight`` parameters.

1. **Strength ratings.** Each club gets an attack and a defence rating relative to the league average
   (1.0 = average), fitted to the last ``window`` days of results. A result counts less the older it is
   (half-life ``half_life`` days) and the fit is schedule adjusted: scoring against a strong defence is
   worth more than scoring against a weak one (iterated until stable, Maher 1982).
2. **Shrinkage.** Ratings are pulled towards a prior with ``shrink`` pseudo-matches, so a small sample
   cannot produce an extreme rating. Clubs that were not in the league last season start from the
   *measured* average of earlier newly promoted clubs, not from 1.0.
3. **Home advantage** is the measured gap between the weighted average home and away goals.
4. **Recent form.** The goals a club scored and conceded in its last ``form_matches`` games are compared
   with what the ratings expected against those opponents; the ratio (shrunk) nudges attack and defence,
   scaled by ``form_weight``.
5. **Head to head.** The same comparison over the last meetings of the two clubs (``h2h_matches``, at most
   ``h2h_years`` old) nudges both goal expectations, scaled by ``h2h_weight``.
6. **Scoreline grid.** Independent Poisson goals with the Dixon-Coles (1997) correction for 0-0, 1-0, 0-1
   and 1-1, then summed into home / draw / away. ``draw_boost`` rescales the draw probability.
"""
import math
from bisect import bisect_left
from collections import defaultdict
from datetime import date

DEFAULTS = {
    "half_life": 365.0,    # days until a result counts half as much
    "window": 1460,        # days of history used (4 seasons)
    "shrink": 6.0,         # pseudo-matches of prior in every rating
    "iterations": 12,      # schedule-adjustment passes
    "form_matches": 5,     # last N matches of each club
    "form_weight": 0.0,    # 0 = form term off (validated: any weight > 0 lowered accuracy, see explain.md)
    "form_prior": 1.5,     # pseudo-goals added to both sides of the form ratio
    "h2h_matches": 6,      # last N meetings of the two clubs
    "h2h_years": 10,       # ignore meetings older than this
    "h2h_weight": 0.0,     # 0 = head-to-head term off (validated the same way)
    "h2h_prior": 4.0,      # pseudo-goals added to both sides of the head-to-head ratio
    "rho": -0.05,          # Dixon-Coles low-score correction (negative = more draws)
    "draw_boost": 1.0,     # multiplies the draw probability before renormalising
    "max_goals": 9,
}

LAMBDA_MIN, LAMBDA_MAX = 0.15, 4.5
DEFAULT_MU = (1.5, 1.2)  # home / away goals when there is no history at all


def to_ordinal(day):
    """A date, an ISO date string or an ordinal day number -> ordinal day number."""
    if isinstance(day, int):
        return day
    return day.toordinal() if isinstance(day, date) else date.fromisoformat(day).toordinal()


def season_of(ordinal):
    day = date.fromordinal(ordinal)
    return day.year if day.month >= 8 else day.year - 1


def poisson_pmf(lam, top):
    pmf = [math.exp(-lam)]
    for k in range(1, top + 1):
        pmf.append(pmf[-1] * lam / k)
    return pmf


def scoreline_grid(lam_home, lam_away, rho=0.0, top=9):
    """P(home goals = i, away goals = j) for i, j in 0..top, renormalised after the Dixon-Coles correction."""
    ph, pa = poisson_pmf(lam_home, top), poisson_pmf(lam_away, top)
    grid = [[ph[i] * pa[j] for j in range(top + 1)] for i in range(top + 1)]
    if rho:
        grid[0][0] *= max(0.0, 1 - lam_home * lam_away * rho)
        grid[0][1] *= max(0.0, 1 + lam_home * rho)
        grid[1][0] *= max(0.0, 1 + lam_away * rho)
        grid[1][1] *= max(0.0, 1 - rho)
    total = sum(map(sum, grid))
    return [[p / total for p in row] for row in grid]


def outcome_probabilities(grid, draw_boost=1.0):
    home = sum(grid[i][j] for i in range(len(grid)) for j in range(i))
    draw = sum(grid[i][i] for i in range(len(grid))) * draw_boost
    away = sum(grid[i][j] for i in range(len(grid)) for j in range(i + 1, len(grid)))
    total = home + draw + away
    return home / total, draw / total, away / total


class Fit:
    """Ratings fitted to the matches before ``as_of`` (an ordinal day)."""

    def __init__(self, as_of, mu_home, mu_away, ratings, newcomer_prior, matches_used):
        self.as_of = as_of
        self.mu_home, self.mu_away = mu_home, mu_away
        self.ratings = ratings              # {team: (attack, defence)}
        self.newcomer_prior = newcomer_prior
        self.matches_used = matches_used

    def rating(self, team, newcomer=False):
        if team in self.ratings:
            return self.ratings[team]
        return self.newcomer_prior if newcomer else (1.0, 1.0)


class MatchModel:
    """Holds the finished matches (dicts with date, home, away, hg, ag) and predicts from any date onwards."""

    def __init__(self, matches, params=None):
        self.params = {**DEFAULTS, **(params or {})}
        rows = sorted(((to_ordinal(m["date"]), m["home"], m["away"], m["hg"], m["ag"])
                       for m in matches if m.get("hg") is not None), key=lambda r: (r[0], r[1]))
        self.rows = rows
        self.ords = [r[0] for r in rows]
        self.by_team = defaultdict(list)     # team -> row indexes
        self.by_pair = defaultdict(list)     # frozenset({a, b}) -> row indexes
        self.season_teams = defaultdict(set)
        for i, (o, home, away, _hg, _ag) in enumerate(rows):
            self.by_team[home].append(i)
            self.by_team[away].append(i)
            self.by_pair[frozenset((home, away))].append(i)
            self.season_teams[season_of(o)].update((home, away))
        self._newcomers = {}

    # ---- newcomers ----------------------------------------------------------
    def is_newcomer(self, team, as_of):
        previous = self.season_teams.get(season_of(as_of) - 1)
        return bool(previous) and team not in previous

    def newcomer_prior(self, as_of):
        """Average (attack, defence) of clubs in their first season back, over finished seasons before ``as_of``."""
        season = season_of(as_of)
        if season in self._newcomers:
            return self._newcomers[season]
        att, dfn = [], []
        for s in sorted(self.season_teams):
            if s >= season or s - 1 not in self.season_teams:
                continue
            goals = defaultdict(lambda: [0, 0, 0])
            for i in self._season_rows(s):
                _o, home, away, hg, ag = self.rows[i]
                goals[home][0] += hg; goals[home][1] += ag; goals[home][2] += 1
                goals[away][0] += ag; goals[away][1] += hg; goals[away][2] += 1
            played = sum(v[2] for v in goals.values())
            if not played:
                continue
            average = sum(v[0] for v in goals.values()) / played
            for team in self.season_teams[s] - self.season_teams[s - 1]:
                gf, ga, n = goals[team]
                if n >= 20 and average:
                    att.append(gf / n / average)
                    dfn.append(ga / n / average)
        att, dfn = att[-30:], dfn[-30:]   # the last ten or so seasons
        prior = (sum(att) / len(att), sum(dfn) / len(dfn)) if att else (1.0, 1.0)
        self._newcomers[season] = prior
        return prior

    def _season_rows(self, season):
        lo = bisect_left(self.ords, date(season, 8, 1).toordinal())
        hi = bisect_left(self.ords, date(season + 1, 8, 1).toordinal())
        return range(lo, hi)

    # ---- ratings ------------------------------------------------------------
    def fit(self, as_of):
        as_of = to_ordinal(as_of)
        p = self.params
        hi = bisect_left(self.ords, as_of)                  # strictly before the date: no same-day leakage
        lo = bisect_left(self.ords, as_of - int(p["window"]))
        rows = self.rows[lo:hi]
        prior_new = self.newcomer_prior(as_of)
        if not rows:
            return Fit(as_of, *DEFAULT_MU, {}, prior_new, 0)

        weights = [0.5 ** ((as_of - r[0]) / p["half_life"]) for r in rows]
        total_w = sum(weights)
        mu_h = sum(w * r[3] for w, r in zip(weights, rows)) / total_w
        mu_a = sum(w * r[4] for w, r in zip(weights, rows)) / total_w
        teams = sorted({r[1] for r in rows} | {r[2] for r in rows})
        index = {t: i for i, t in enumerate(teams)}
        n = len(teams)
        prior_a = [prior_new[0] if self.is_newcomer(t, as_of) else 1.0 for t in teams]
        prior_d = [prior_new[1] if self.is_newcomer(t, as_of) else 1.0 for t in teams]
        k_goals = p["shrink"] * (mu_h + mu_a) / 2
        prepared = [(index[r[1]], index[r[2]], r[3], r[4], w) for r, w in zip(rows, weights)]
        exposure = [0.0] * n
        for h, a, _hg, _ag, w in prepared:
            exposure[h] += w
            exposure[a] += w

        att, dfn = list(prior_a), list(prior_d)
        for _ in range(int(p["iterations"])):
            num_a, den_a = [k_goals * x for x in prior_a], [k_goals] * n
            num_d, den_d = [k_goals * x for x in prior_d], [k_goals] * n
            for h, a, hg, ag, w in prepared:
                num_a[h] += w * hg;  den_a[h] += w * mu_h * dfn[a]
                num_a[a] += w * ag;  den_a[a] += w * mu_a * dfn[h]
                num_d[h] += w * ag;  den_d[h] += w * mu_a * att[a]
                num_d[a] += w * hg;  den_d[a] += w * mu_h * att[h]
            att = [num_a[i] / den_a[i] for i in range(n)]
            dfn = [num_d[i] / den_d[i] for i in range(n)]
            share = sum(exposure)
            mean_a = sum(att[i] * exposure[i] for i in range(n)) / share
            mean_d = sum(dfn[i] * exposure[i] for i in range(n)) / share
            att = [x / mean_a for x in att]
            dfn = [x / mean_d for x in dfn]
        return Fit(as_of, mu_h, mu_a, {t: (att[i], dfn[i]) for t, i in index.items()}, prior_new, len(rows))

    # ---- form and head to head ------------------------------------------------
    def _expected(self, fit, home, away, newcomer_home, newcomer_away):
        att_h, def_h = fit.rating(home, newcomer_home)
        att_a, def_a = fit.rating(away, newcomer_away)
        return fit.mu_home * att_h * def_a, fit.mu_away * att_a * def_h

    def form_ratios(self, team, fit, as_of, p=None):
        """(attack, defence) form ratios: goals in the last matches / goals the ratings expected. 1.0 = as expected."""
        p = p or self.params
        as_of = to_ordinal(as_of)
        hi = bisect_left([self.ords[i] for i in self.by_team[team]], as_of)
        recent = self.by_team[team][max(0, hi - int(p["form_matches"])):hi]
        if not recent:
            return 1.0, 1.0
        gf = ga = ef = ea = 0.0
        for i in recent:
            o, home, away, hg, ag = self.rows[i]
            exp_h, exp_a = self._expected(fit, home, away, self.is_newcomer(home, o), self.is_newcomer(away, o))
            if home == team:
                gf += hg; ga += ag; ef += exp_h; ea += exp_a
            else:
                gf += ag; ga += hg; ef += exp_a; ea += exp_h
        c = p["form_prior"]
        return (gf + c) / (ef + c), (ga + c) / (ea + c)

    def head_to_head(self, home, away, fit, as_of, p=None):
        """Meetings of the two clubs before ``as_of`` plus (home, away) goal ratios against what the ratings expected."""
        p = p or self.params
        as_of = to_ordinal(as_of)
        indexes = [i for i in self.by_pair.get(frozenset((home, away)), [])
                   if as_of - 365 * p["h2h_years"] <= self.rows[i][0] < as_of][-int(p["h2h_matches"]):]
        meetings = []
        sums = {"home_goals": 0.0, "away_goals": 0.0, "home_exp": 0.0, "away_exp": 0.0}
        record = {"home_wins": 0, "draws": 0, "away_wins": 0}
        for i in indexes:
            o, h, a, hg, ag = self.rows[i]
            exp_h, exp_a = self._expected(fit, h, a, self.is_newcomer(h, o), self.is_newcomer(a, o))
            ours, theirs = (hg, ag) if h == home else (ag, hg)       # goals by the club that is at home *now*
            e_ours, e_theirs = (exp_h, exp_a) if h == home else (exp_a, exp_h)
            sums["home_goals"] += ours; sums["away_goals"] += theirs
            sums["home_exp"] += e_ours; sums["away_exp"] += e_theirs
            record["home_wins" if ours > theirs else "away_wins" if ours < theirs else "draws"] += 1
            meetings.append({"date": date.fromordinal(o).isoformat(), "home": h, "away": a, "hg": hg, "ag": ag})
        c = p["h2h_prior"]
        ratios = ((sums["home_goals"] + c) / (sums["home_exp"] + c), (sums["away_goals"] + c) / (sums["away_exp"] + c))
        return {"n": len(indexes), "ratios": ratios, "record": record, "meetings": meetings[::-1],
                "goals": (sums["home_goals"], sums["away_goals"])}

    def recent(self, team, as_of, count=5):
        """The club's last ``count`` finished matches before ``as_of``, newest first, as (date, home, away, hg, ag)."""
        as_of = to_ordinal(as_of)
        indexes = self.by_team.get(team, [])
        hi = bisect_left([self.ords[i] for i in indexes], as_of)
        return [(date.fromordinal(self.rows[i][0]).isoformat(),) + self.rows[i][1:] for i in indexes[max(0, hi - count):hi]][::-1]

    # ---- the prediction -----------------------------------------------------------
    def predict(self, home, away, as_of, fit=None, params=None):
        """Probabilities for ``home`` v ``away`` on ``as_of`` using only matches before that date."""
        p = {**self.params, **(params or {})}
        as_of = to_ordinal(as_of)
        fit = fit or self.fit(as_of)
        new_h, new_a = self.is_newcomer(home, as_of), self.is_newcomer(away, as_of)
        att_h, def_h = fit.rating(home, new_h)
        att_a, def_a = fit.rating(away, new_a)

        form_h = self.form_ratios(home, fit, as_of, p) if p["form_weight"] else (1.0, 1.0)
        form_a = self.form_ratios(away, fit, as_of, p) if p["form_weight"] else (1.0, 1.0)
        w = p["form_weight"]
        lam_h = fit.mu_home * att_h * form_h[0] ** w * def_a * form_a[1] ** w
        lam_a = fit.mu_away * att_a * form_a[0] ** w * def_h * form_h[1] ** w

        h2h = self.head_to_head(home, away, fit, as_of, p) if p["h2h_weight"] or p.get("with_h2h_detail") else None
        if h2h and p["h2h_weight"] and h2h["n"]:
            lam_h *= h2h["ratios"][0] ** p["h2h_weight"]
            lam_a *= h2h["ratios"][1] ** p["h2h_weight"]

        lam_h = min(max(lam_h, LAMBDA_MIN), LAMBDA_MAX)
        lam_a = min(max(lam_a, LAMBDA_MIN), LAMBDA_MAX)
        grid = scoreline_grid(lam_h, lam_a, p["rho"], int(p["max_goals"]))
        win, draw, loss = outcome_probabilities(grid, p["draw_boost"])
        return {
            "probs": {"home": win, "draw": draw, "away": loss},
            "xg": {"home": lam_h, "away": lam_a},
            "grid": grid,
            "ratings": {"home": (att_h, def_h), "away": (att_a, def_a)},
            "form": {"home": form_h, "away": form_a},
            "h2h": h2h,
            "newcomer": {"home": new_h, "away": new_a},
        }


def scoreline_summary(grid, top=3):
    """Most likely scores and the usual side markets from a scoreline grid."""
    size = len(grid)
    cells = sorted(((grid[i][j], i, j) for i in range(size) for j in range(size)), reverse=True)[:top]
    over25 = sum(grid[i][j] for i in range(size) for j in range(size) if i + j > 2)
    btts = sum(grid[i][j] for i in range(1, size) for j in range(1, size))
    return {
        "top_scores": [{"home": i, "away": j, "p": p} for p, i, j in cells],
        "over_2_5": over25,
        "btts": btts,
        "clean_sheet": {"home": sum(grid[i][0] for i in range(size)), "away": sum(grid[0][j] for j in range(size))},
    }
