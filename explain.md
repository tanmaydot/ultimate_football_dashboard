# Match and player predictions: what was built and how it works

This document covers the prediction features added to the dashboard: what changed, where the data comes from, how the
algorithms work (with the arithmetic), how they were tested against history, what the numbers say, what is missing, and
how to carry on improving it. Every figure quoted here was produced by the code in this repository
(`python manage.py backtest_predictions`, stored in `ufd/ufd_app/data/pl_validation.json`).

## 1. Summary

| | |
|---|---|
| **Match forecasts** | Win / draw / loss probabilities, expected goals, most likely scores, "3+ goals", "both teams score" and clean-sheet chances for all 311 remaining Premier League fixtures. |
| **Player forecasts** | For every squad player with match data (476 of 545): expected minutes, chance to score, to assist, to do either, expected match rating, and a hot / steady / cold label, all for the club's next match. |
| **History used** | 26 finished seasons (2000-01 to 2025-26) plus the current one: 9,930 matches from the open-source OpenFootball repository. |
| **Honest scorecard (match model)** | On 3,869 matches from 2016-17 to today, which were **not used to choose any parameter**: 54.1% of results called correctly against 44.5% for a fixed home/draw/away guess; ranked probability score 0.1989 against 0.2329 (the gain is 0.034, 95% interval 0.030 to 0.038). Probabilities are well calibrated. |
| **Honest scorecard (player model)** | On 29 held-out matches (76 scorers): clearly better than naive baselines (log loss 0.184 against 0.213 for a constant rate) and a little better than a strong "position average x expected minutes" baseline (0.188). The ranking is useful (the real scorer was in the model's top three 62% of the time against 20% by chance), but there are only 76 scorers, so treat the player numbers as indicative. |
| **One finding that contradicts the brief** | The brief asked for recent form and head-to-head records as inputs. Both are computed and shown, but testing showed that feeding them into the odds makes the forecasts **worse**, so they are deployed with weight 0 (section 5.4). They remain switchable. |
| **Tests** | 411 tests, all passing (126 new). No new dependency: the models are plain Python. |

## 2. What changed

### New files (all under `ufd/ufd_app/` unless stated)

| File | Purpose |
|---|---|
| `openfootball.py` | Downloads and parses the OpenFootball season files (two text layouts), keeps a cache, validates each season. |
| `predict_match.py` | The match model (pure Python, no Django): ratings, form, head to head, scoreline grid. |
| `backtest.py` | Leak-free walk-forward replay, scoring metrics, baselines, bootstrap intervals, parameter tuning. |
| `predict_player.py` | The player model and its walk-forward validation. |
| `predictions.py` | Glue: reads the project's data, merges results, builds `pl_predictions.json`, insights text, helpers for the pages. |
| `validation.py` | Runs both validations and writes `pl_validation.json`. |
| `management/commands/build_predictions.py` | Downloads OpenFootball, builds the predictions file. |
| `management/commands/backtest_predictions.py` | Scores the models on history (`--tune` re-selects the match parameters). |
| `templates/predictions.html`, `partials/prob_bar.html`, `form_pills.html`, `form_tag.html`, `sortable.html` | The Predictions page and its reusable pieces. |
| `data/openfootball.json` (0.6 MB), `data/pl_predictions.json` (1.2 MB), `data/pl_validation.json` (16 KB) | Generated data. |
| `.github/workflows/refresh-predictions-validation.yml` | Monthly re-scoring. |
| `test_openfootball.py`, `test_predict_match.py`, `test_backtest.py`, `test_predict_player.py`, `test_predictions.py`, `test_prediction_commands.py` | 126 new tests. |

### Changed files

* `dashboard.py`: new "Next match" widget, `team_lookup`, `next_match_card`; position grouping moved to `pl_data.POSITION_GROUPS` so the pages and the player model share one definition.
* `team_pages.py`: the Predictions view and context; the squad page and player page gain forecast columns / a forecast section.
* `urls.py`: `/dashboard/<id>/predictions/`.
* `templates/squad.html`, `player.html`, `dashboard.html`, `partials/nav_links.html`, `partials/head.html`: UI (section 8). The table-sorting script moved to a partial shared by the squad and predictions pages (it was inline in `squad.html`).
* `management/commands/check_data.py`: also validates the predictions file (probabilities add up) and the OpenFootball archive (every finished season has 380 matches).
* `util.py`: `DATA_DIR`. `.gitattributes`: the new data files use `merge=binary` like the other generated files.
* `.github/workflows/refresh-data.yml`: runs `build_predictions` after `build_pl_data`.
* Tests: one existing assertion changed (the dashboard now has 7 widget templates, not 6).

The existing architecture is unchanged: the site still renders from pre-built JSON files and makes no API calls or model runs while serving a request.

## 3. Data sources

| Source | Used for | Licence / access | Coverage |
|---|---|---|---|
| **OpenFootball England** ([github.com/openfootball/england](https://github.com/openfootball/england)) | Fixtures and results for team form, strength and head to head; the list of remaining fixtures | CC0 (public domain), free, no key | Premier League 2000-01 to 2026-27, 380 matches per finished season. Used from the raw files; checked at download (20 clubs, 380 matches, no duplicate pairing). |
| **football-data.org** (already in the project) | Latest results, laid over OpenFootball because OpenFootball can lag by days (19 results were missing on the build day) | Existing API key | Current season |
| **PitchAPI** (already in the project) | Player lines for every finished match: minutes, goals, assists, shots, xG, xA, rating, team | Existing API key | Current season only (69 matches stored) |

### What is unavailable or incomplete (stated plainly)

* **Bookmaker odds**: no free open source is available here, so there is no market benchmark. A market-beating forecast would need them; this model's edge over a naive guess is established, its standing against bookmakers is not.
* **Injuries, suspensions and confirmed line-ups**: not available before kick-off. Availability is inferred from recent minutes (a match missed counts as zero minutes).
* **Player data before the current season**: PitchAPI lines exist only for matches already in `pl_history.json`. Earlier seasons' appearances, assists and ratings were not found in any free, reliable, open source. OpenFootball's 2025-26 file lists goal scorers (519 lines) but only goals: no appearances, assists or ratings, and no other season has them. It is not used (see future improvements).
* **Assists and ratings in OpenFootball**: results only.
* **xG for the match model**: not used; the model works from goals. PitchAPI xG is used only inside the player model.

## 4. Architecture

```
                      daily GitHub Action (22:30 UTC)                 monthly Action
  football-data.org ─┐                                                    │
  PitchAPI ──────────┼─> build_pl_data ─> pl_teams.json, pl_history.json  │
  footballdata.io ───┘                         │                          │
                                               ▼                          ▼
  OpenFootball ───> openfootball.py ─> openfootball.json   backtest_predictions ─> pl_validation.json
  (raw .txt files)                          │                          │
                                            ▼                          │
                                   build_predictions  <────────────────┘ (embeds the track record)
                              ┌──────────────┴──────────────┐
                    predict_match.py                predict_player.py
                    (team ratings, W/D/L)           (player rates, minutes, rating)
                              └──────────────┬──────────────┘
                                             ▼
                                   pl_predictions.json
                                             │   read by dashboard.py / team_pages.py (mtime-cached)
                                             ▼
         Dashboard widget · Predictions page · Squad columns · Player section
```

Why precompute: it matches how the rest of the project works, keeps requests instant and keyless, and makes the forecasts
reproducible (the file is committed and Render redeploys on every data commit). Why the models are pure Python modules with
no Django imports: they can be replayed on history in the backtest with exactly the code that makes live forecasts.

## 5. Match prediction algorithm

Code: `predict_match.py`. It estimates how many goals each side will score, then turns that into outcome probabilities.

### 5.1 Strength ratings

For a prediction on date D, take every league match in the 4 years before D (strictly earlier dates: a match played the same
day is excluded). Each match gets a weight that halves every `half_life` = 365 days: `w = 0.5^(age_days / 365)`.

* League averages: `mu_home = sum(w * home_goals) / sum(w)` and `mu_away` likewise. Home advantage is whatever the data says (currently 1.57 against 1.31 goals).
* Each club gets an **attack** `a` and a **defence** `d`, relative to the league (1.00 = average; for defence, lower is better). They solve, over all weighted matches, the schedule-adjusted equations:

  `a_t = (sum w * goals_scored + K * prior_a) / (sum w * mu * d_opponent + K)`  and  `d_t = (sum w * goals_conceded + K * prior_d) / (sum w * mu * a_opponent + K)`

  where `mu` is `mu_home` or `mu_away` depending on the venue. The pair is iterated 12 times and renormalised to average 1. Scoring against a strong defence therefore counts for more than scoring against a weak one.
* **Shrinkage**: `K = shrink * average_goals`, with `shrink` = 6 pseudo-matches, pulls every rating towards its prior so a small sample cannot produce an extreme rating.
* **Newly promoted clubs** (not in the league last season) use a prior measured from history instead of 1.00: for each earlier season, the goals scored and conceded per match of the clubs that had just come up, relative to the league average, averaged over the last ~10 seasons. Today that gives attack 0.70, defence 1.24 (promoted clubs score about 30% fewer and concede about 24% more than average). This is computed from data before the prediction date, not assumed.

### 5.2 Expected goals

`lambda_home = mu_home * a_home * d_away`    `lambda_away = mu_away * a_away * d_home`  (clipped to 0.15 to 4.5)

### 5.3 From goals to W / D / L

Goals are Poisson: `P(i, j) = Pois(i; lambda_home) * Pois(j; lambda_away)` over 0 to 9 goals each, with the Dixon-Coles (1997) correction
`rho = -0.05` applied to 0-0, 1-0, 0-1 and 1-1 (it nudges low scores, where independent Poisson is least accurate), then renormalised.
`home = sum of cells with i > j`, `draw = sum of the diagonal`, `away = the rest`. The same grid gives the most likely scores, "3+ goals", "both teams score" and clean-sheet chances.

### 5.4 Recent form and head to head (computed, tested, deployed at weight 0)

* **Form**: over each club's last 5 matches, `form_ratio = (goals scored + 1.5) / (goals the ratings expected against those opponents + 1.5)`; the attack/defence ratings are multiplied by `ratio ^ form_weight`.
* **Head to head**: over the last up to 6 meetings (at most 10 years old), the same comparison of goals scored against goals the ratings expected, with 4 pseudo-goals of shrinkage; both lambdas are multiplied by `ratio ^ h2h_weight`.

Both compare against the *ratings' expectation*, not raw goals, so they measure what the ratings miss rather than double counting team strength.

**Result of testing them** (mean ranked probability score on the 11 tuning seasons 2005-06 to 2015-16; lower is better, 0.1963 with the term off):

| Weight | 0 | 0.05 | 0.1 | 0.2 | 0.3 |
|---|---|---|---|---|---|
| Form (last 5 matches) | 0.1963 | 0.1964 | 0.1966 | 0.1975 | 0.1988 |
| Head to head (last 6) | 0.1963 | 0.1963 | 0.1964 | 0.1967 | 0.1978 (0.4) |

The score gets monotonically worse as either weight rises, for every window length tried (form 3, 5, 8 and 10 matches; head to head 4, 6 and 10 meetings), and the same on the 2016-2026 hold-out (section 9.2). The time-decayed ratings already weight recent results more heavily, and a short window adds noise faster than signal. A shorter half-life (240 days) did not help either (0.1965). So: form and head to head are **shown** in the UI as insights (a reader finds them useful and they are true statements), and are **not** fed into the probabilities. Set `form_weight` / `h2h_weight` in `predict_match.DEFAULTS` to switch them on.

### 5.5 How the parameters were chosen

`backtest.tune` searched one group at a time, scoring only the 11 tuning seasons (2005-06 to 2015-16):

| Parameter | Searched | Chosen |
|---|---|---|
| Half-life / shrinkage | 180, 365, 730 days x 3, 6, 12 pseudo-matches | **365 days, 6** |
| Form weight, window | 0 to 1, 4 / 6 / 10 matches | **0** |
| Head-to-head weight, window | 0 to 1, 4 / 6 / 10 meetings | **0** |
| Dixon-Coles rho, draw boost | rho 0 to -0.15, boost 1.0 to 1.15 | **rho -0.05, boost 1.0** (no measurable difference between options: the effect is below 0.0001) |

Seasons 2016-17 onwards were never read during tuning.

### 5.6 Worked example (real build, 20 Oct 2026)

Ipswich Town (home) against Nottingham Forest, 23 Oct 2026. The model has 1,483 weighted matches; `mu_home` = 1.568, `mu_away` = 1.312.
Ipswich came up this season: their prior is attack 0.70 / defence 1.24, and their results so far moved them to attack 0.805 / defence 1.435 (leaky). Forest: attack 0.902, defence 0.950.

* `lambda_home = 1.568 x 0.805 x 0.950 = 1.20`
* `lambda_away = 1.312 x 0.902 x 1.435 = 1.70`
* Poisson grid with the correction gives **home 26.5%, draw 25.1%, away 48.5%**, most likely score 1-1 (11.8%), 3+ goals 55%, both teams score 58%.

## 6. Player prediction algorithm

Code: `predict_player.py`. One `PlayerTracker` holds a running record per player (minutes, goals, assists, xG, xA, rating for every club match since he first appeared, with a 0-minute entry for matches he was not in the squad). The same tracker produces live forecasts and, replayed match by match, the backtest.

For one player and one upcoming match:

1. **Expected minutes** = recency-weighted mean of the club's last 8 matches (half-life 3 matches), absent matches counting 0. `p_play` is the weighted share of those matches he played in.
2. **Rates per 90** for goals and for assists, each a blend of three estimates:
   * *season*: `(his goals + k90 * position_rate) / (his 90s + k90)` with `k90` = 2 pseudo-matches of position average (a small sample is pulled to the position's average rate);
   * *recent* (the most recent matches): the same formula over his last 5 appearances weighted by recency (half-life 2.5 appearances), `k90_recent` = 6;
   * *xG / xA*: the same season and recent rates from expected goals / assists, rescaled so league xG equals league goals (only once there are at least 10 goals, otherwise the ratio is untrustworthy).

   `rate = (1 - xg_weight) * goals_estimate + xg_weight * xg_estimate`, where each estimate is `(1 - recent_weight) * season + recent_weight * recent`; `xg_weight` = 0.8, `recent_weight` = 0.15.
3. **Context**: `lambda = rate / 90 * expected_minutes * (opponent_defence * venue_factor) ^ context_exponent`, where `opponent_defence` is the opponent's defence rating from the match model, `venue_factor` is the league's home or away goal level over its average, and `context_exponent` = 1.
4. **Probabilities**: `P(goal) = 1 - exp(-lambda_goals)`, `P(assist) = 1 - exp(-lambda_assists)`, `P(goal or assist) = 1 - exp(-(lambda_goals + lambda_assists))`.
5. **Expected rating**: recency-weighted mean of his ratings (only matches of 15+ minutes; half-life 2 appearances), shrunk towards his position's mean with 4 pseudo-appearances, plus `0.3 x (his team's expected goal advantage in the match)`. Clamped to 4 to 10.
6. **Form label** (hot / steady / cold): average of his last three ratings minus his season mean; at least 0.35 above is *hot*, at least 0.35 below is *cold*; needs 4 rated appearances. **Confidence**: high from 900 minutes played, medium from 270, otherwise low.

"Using his most recent match and historical season statistics" is realised as: season totals (the whole-season rate), the recent-matches blend, the minutes and rating averages (which are dominated by the last two or three matches), and the last match shown next to every forecast.

**Parameters** were tuned on the first 40 of the 69 stored matches (log loss on goals and assists; mean absolute error for ratings) and then scored on the remaining 29 as a hold-out. One deliberate departure: the tuned `recent_weight` was 0; it is deployed at 0.15 because validation cannot tell 0 from 0.3 apart (identical scores) and the brief asks for the most recent match to count. Worked example: Emersonn (Ipswich), 7 appearances, 470 minutes: 0.35 goals and 0.09 assists per 90, expected 70 minutes, so `lambda_goals` = 0.283 and P(goal) = 1 - e^-0.283 = 24.7%; expected rating 6.6, last three ratings 0.42 below his average, so "cold"; confidence medium.

## 7. Avoiding data leakage

* The match model's `fit`, `form_ratios` and `head_to_head` all read only rows with a date **strictly before** the prediction date (binary search on dates), so a match played the same day is excluded too.
* The backtest refits the ratings every 3 days, always on data older than the refit date, so forecasts may be up to 3 days stale but never early.
* Newly-promoted priors and "was the club here last season" use only seasons that finished before the prediction.
* The constant baseline uses only the three years before each match.
* Parameters were chosen on seasons that are excluded from the reported test.
* The player model forecasts a match before learning it (`walk_forward` predicts, then calls `observe`); the opponent ratings used for context are fitted before that match's date.
* Tests prove each of these by wrecking the future and checking nothing earlier moves (`LeakageTests`, `WalkForwardTests`, `test_replay_never_uses_future_results`, `test_predictions_for_a_match_never_depend_on_that_match_or_later_ones`).

## 8. The interface

* **Predictions page** (`/dashboard/<club>/predictions/`, "Predictions" in the sidebar; the sidebar's club switcher keeps you on it). Three tabs:
  * *Club fixtures*: a next-match card (crests, most likely score, a home / draw / away bar with percentages, expected goals, 3+ goals, both teams score, clean sheet, plain-language **match insights**, previous meetings), the following fixtures as a list with a mini bar each, recent form pills, attack / defence rating with league rank. A fixture whose date has passed with no result is shown as "waiting for a result", never as upcoming.
  * *Next matchday, all clubs*: every fixture of the coming matchday with its bar; club names link to that club's predictions.
  * *Player outlook*: top-three scoring chances, then a sortable table (expected minutes, goal %, assist %, goal-or-assist %, expected rating, hot / steady / cold, last match, confidence).
* **Squad page**: new Goal %, Assist %, Exp. rating and Form columns, sortable like the rest.
* **Player page**: a "Next match outlook" section with the same numbers, rates per 90, chance of playing and last match.
* **Dashboard**: a "Next match" widget. Saved widget layouts are reset once (layout key bumped to v12) so the new widget appears.
* **Track-record banner** on the Predictions page shows the hold-out numbers, and a "How these numbers are made, and what is missing" panel states the limits. The probability bars have text labels and `aria-label`s, so meaning never relies on colour alone.

## 9. Testing and validation

### 9.1 Test suite

411 tests pass (`python manage.py test`, about 14 s). The 126 new ones cover:

* **Data pipeline**: both OpenFootball layouts, year inference (including June 2020), matchday headers, unplayed fixtures, scorer lines ignored, club-name variants, empty and garbage input, sanity checks (20 clubs, 380 matches, no duplicate pairing), archive round trip, download only what is missing, failed or malformed downloads keep the earlier copy, and the committed archive (every finished season complete, plausible scores). Result merging: project results fill OpenFootball's gaps and win on disagreement; unknown clubs skipped.
* **Match algorithm**: probabilities valid, home advantage measured, no-history and unknown-club cases, goal clipping, newcomer prior learned from data, time decay, form and head-to-head effects (and that they are off by default).
* **Leakage**: results on or after the date, the match itself and same-day matches cannot change a forecast.
* **Accuracy**: metrics checked against hand-computed values; the model beats a constant forecast on a league with real strength differences and gains nothing on a league of equal clubs; a real-data regression test replays 2022-23 and 2023-24 from the committed archive (RPS at least 0.015 better than the baseline, accuracy above 50%, calibration within 0.07).
* **Player algorithm**: shrinkage, cameo handling, minutes, xG and recency effects, opponent context, ratings, labels, edge cases (never seen, no ratings, no assists in the sample), scorers found in a simulated league, and validation helpers.
* **Builder and UI**: document structure, determinism, overdue fixtures, players predicted for the next real fixture only, every page with and without a predictions file, unknown clubs, sidebar and nav links, widget, squad columns, player section, data checker rules, and the commands.

### 9.2 Match model against history

Walk-forward replay of 2016-17 to 2026-27 to date: **3,869 matches**, parameters fixed beforehand. Each score is a mean over matches.

| Forecast | Right result | RPS | Log loss | Brier |
|---|---|---|---|---|
| **Model** | **54.1%** | **0.1989** | **0.968** | **0.575** |
| Constant home / draw / away rates (previous 3 years) | 44.5% | 0.2329 | 1.066 | 0.645 |
| Model with form term on (weight 0.2) | 54.1% | 0.1993 | 0.969 | 0.576 |
| Model with head-to-head term on (weight 0.2) | 54.1% | 0.1996 | 0.970 | 0.576 |
| Without Dixon-Coles / draw boost | 54.0% | 0.1989 | 0.968 | 0.575 |
| Without time decay | 53.2% | 0.2013 | 0.975 | 0.579 |
| Without shrinkage | 54.2% | 0.1996 | 0.971 | 0.576 |

Paired bootstrap, mean RPS of the model minus the alternative (negative = model better), with 95% interval:

| Against | Difference | 95% interval | Reading |
|---|---|---|---|
| Constant rates | -0.0340 | -0.0378 to -0.0302 | Clearly better |
| Form term on | -0.0004 | -0.0013 to +0.0005 | Form adds nothing (and costs a little) |
| Head-to-head term on | -0.0007 | -0.0012 to -0.0002 | Head to head makes it significantly worse |
| Without Dixon-Coles | +0.00002 | -0.00006 to +0.00009 | No effect either way |
| Without time decay | -0.0024 | -0.0034 to -0.0014 | Decay helps |
| Without shrinkage | -0.0007 | -0.0015 to +0.0001 | Helps slightly |

Goals: mean predicted home / away goals 1.545 / 1.261 against actual 1.556 / 1.280, so there is no systematic bias.

**Calibration** (all three outcomes pooled; predicted against how often it happened): 0.1 to 0.2 predicted 0.159, observed 0.134; 0.3 to 0.4: 0.346 / 0.343; 0.4 to 0.5: 0.449 / 0.448; 0.5 to 0.6: 0.546 / 0.557; 0.6 to 0.7: 0.645 / 0.691; 0.7 to 0.8: 0.742 / 0.774; 0.8 to 0.9: 0.836 / 0.890. The middle is excellent. At the extremes the model is mildly under-confident about favourites (a "74%" favourite wins about 77%) and slightly over-rates outsiders.

**By season** (right result / RPS): 2016-17 58.7% / 0.192; 2017-18 54.0% / 0.190; 2018-19 59.2% / 0.188; 2019-20 53.4% / 0.199; 2020-21 50.5% / 0.216 (no crowds); 2021-22 54.0% / 0.194; 2022-23 52.9% / 0.210; 2023-24 59.0% / 0.189; 2024-25 53.2% / 0.201; 2025-26 46.6% / 0.210; 2026-27 (50 matches so far) 52.2% / 0.203. Season-to-season swings of five or more points in accuracy are normal for 380 matches; the model beats the constant forecast in every season.

### 9.3 Player model against the stored matches

69 matches, 2,770 player-match rows. Tuned on matches 4 to 40 (37 matches, 69 scorers); **hold-out: matches 41 to 69 (29 matches, 76 scorers)**. The forecast for each match used only earlier matches.

| Goal forecast (hold-out) | Log loss | Brier | AUC | Real scorer in top 3 |
|---|---|---|---|---|
| **Model** | **0.1838** | **0.0455** | **0.796** | **61.9%** |
| Season totals only (no recent, no xG) | 0.1966 | 0.0478 | 0.779 | 45.2% |
| Without xG | 0.1944 | 0.0475 | 0.780 | 45.2% |
| Without recent matches | 0.1838 | 0.0455 | 0.796 | 59.5% |
| Without opponent context | 0.1836 | 0.0454 | 0.797 | 61.9% |
| Baseline: position average x expected minutes | 0.1879 | 0.0462 | 0.782 | n/a |
| Baseline: one constant rate for everyone | 0.2131 | 0.0496 | 0.434 | 20.4% (random) |
| Baseline: his goals per club match so far | 0.2478 | 0.0570 | 0.418 | n/a |

| Rating forecast (hold-out, 727 player-matches) | Mean absolute error |
|---|---|
| **Model** | **0.645** |
| Position average | 0.655 |
| His season mean | 0.713 |
| His last rating | 0.883 |

What this says, without flattering it: expected goals from shots (xG) is the one ingredient that clearly helps; the model is much better than any "what has he done so far" baseline; but most of the signal is *position and minutes* (the position-average baseline is already strong), and beyond that the model adds a modest, consistent improvement. Opponent context and the most recent matches are neutral on this sample (the differences are in the third decimal), and individual match ratings are mostly noise (a standard deviation of 0.81 across 1,852 rated appearances), so a rating forecast can only beat the position average by a hair. With 76 scorers, intervals are wide: do not read a 0.002 difference as real.

### 9.4 Edge cases covered

No history, unknown clubs, fixtures for clubs not in the league file (reported, not forecast), results missing from OpenFootball (filled from the project's data), fixtures whose date has passed with no result (flagged "awaiting"), a season with nothing left to play, no history file, no assists or no ratings in a sample (AUC undefined), players never seen, players with a 5-minute cameo goal, mid-season transfers, a missing or corrupt predictions file (the pages say how to build it), and club ids that are not in the current data (tests use fake clubs).

### 9.5 Not verified

The pages were verified through Django's test client and an HTML-balance check; they were not viewed in a real browser, because the styling comes from CDN scripts (Tailwind, Font Awesome, GridStack) that could not be loaded where this was built. The layout CSS follows the existing page conventions and includes a narrow-screen rule, but it should be eyeballed once.

## 10. Implementation steps (in order)

1. Read the project and kept its architecture: JSON files built by commands, read-only views, pure-Python data modules.
2. Wrote the OpenFootball parser; checked it on all 27 seasons (380 matches, 20 clubs each) and cached them.
3. Wrote the match model; built the leak-free walk-forward harness and scoring before tuning anything.
4. Tuned on 2005-06 to 2015-16 only; discovered that form and head to head do not help, ran finer grids to be sure, and recorded the result instead of forcing them in.
5. Evaluated on the untouched 2016 onwards seasons, with baselines, ablations, bootstrap intervals and calibration.
6. Wrote the player model around a tracker shared by live forecasts and validation; chose parameters on the first 40 matches and scored the rest.
7. Wrote the builder (`predictions.py`), merged project results over OpenFootball, generated insights.
8. Built the UI on the existing patterns and moved shared pieces (sorting script, position groups) so there is one copy of each.
9. Wrote the tests, fixed what they found (see below), extended the data checker and workflows.

Bugs the tests or checks found and fixed: team-name aliases could shadow a club's main key when merging results (project results then failed to line up with OpenFootball's fixtures); xG was rescaled to zero in a league with no goals yet (now needs 10 goals); the player report crashed when no assist had happened (AUC undefined); fixtures dated before the build day with no result were listed as upcoming; the dashboard's filters raised on a club missing from a lookup.

## 11. Running it

```bash
cd ufd
python manage.py build_predictions                 # downloads the current OpenFootball season, then builds pl_predictions.json
python manage.py build_predictions --offline       # no download, uses the cached archive
python manage.py backtest_predictions              # scores both models on history (about a minute) -> pl_validation.json
python manage.py backtest_predictions --tune       # also re-selects the match parameters on 2005-06 to 2015-16 (a few minutes)
python manage.py check_data                        # includes the new files
python manage.py test
```

* The daily workflow now runs `build_predictions` after `build_pl_data`. A monthly workflow re-runs `backtest_predictions` so the banner on the Predictions page stays current.
* No new Python packages and no API key are needed. If OpenFootball cannot be reached, the build keeps the cached copy.
* If the Render deployment pulls a committed `pl_predictions.json` that is older than the data, the pages still work; the page header shows the "as of" date.

## 12. Limitations

* Forecasts are probabilities, not certainties: a 48% favourite loses more often than it wins. No bookmaker comparison is possible here.
* The player model learns from one partial season; confidence is shown per player, and 69 squad players without any match data get no forecast.
* Squad rotation, injuries and suspensions are invisible until they show up in minutes.
* Favourites are slightly under-rated (section 9.2).
* The strength of a newly promoted club is estimated from earlier promoted clubs; the first few matches of a new season lean on that.
* The model ignores xG, shots, transfers, managerial changes, rest days, European fixtures and competition context.
* `pl_predictions.json` is 1.2 MB, mostly insights text and scoreline grids' summaries; fine for Render, but worth slimming if it grows.

## 13. Future improvements (most valuable first)

1. **Add bookmaker odds as a benchmark** (for example Football-Data.co.uk, if reachable from the build) and judge the model against the market.
2. **Calibrate the extremes**: a one-parameter probability "temperature" fitted on the tuning seasons would fix the mild under-confidence about favourites.
3. **Use xG at match level**: PitchAPI / footballdata.io xG for finished matches could replace goals as the model's input (xG is a steadier measure of strength) once a few seasons are stored.
4. **Player history across seasons**: keep each season's `pl_history.json` (it is archived on a new season) and learn player rates over several seasons; use OpenFootball's goal-scorer lines (2025-26 only so far) as a cold-start prior with name matching.
5. **Availability**: add injury and suspension feeds if a free source appears, and replace the "minutes from recent matches" guess with it.
6. **Ensembles and a second family of models** (Elo, a bivariate Poisson with a draw parameter, or a gradient-boosted classifier on the same leak-free features) compared on the same walk-forward harness; keep a model only if it beats this one on RPS with a confidence interval that excludes zero.
7. **Re-tune each summer** with `backtest_predictions --tune`, and re-test whether form and head to head help with more data.
8. **Season simulation**: use the stored scoreline grids to simulate the table and show title, European and relegation odds.
9. **Frontend polish**: a real-browser pass, a proper line-up-aware "predicted XI" view, and a slimmer predictions file (drop scoreline details the UI does not show).
