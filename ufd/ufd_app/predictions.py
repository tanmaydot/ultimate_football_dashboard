"""Builds ``data/pl_predictions.json``: win/draw/loss for every remaining fixture and next-match outlooks for players.

Inputs are the files the rest of the project already keeps (``pl_teams.json``, ``pl_history.json``) plus the
OpenFootball archive (``openfootball.json``). Nothing is fetched here; ``build_predictions`` downloads, this
module only computes, so it is easy to test. The dashboard reads the JSON file and never calls a model itself.
"""
import logging
from datetime import date

from . import predict_player as pp
from .openfootball import current_season_start, season_label
from . import pl_data
from .pl_data import normalise_team_name
from .predict_match import DEFAULTS, MatchModel, scoreline_summary, to_ordinal
from .util import DATA_DIR, read_json, utc_now_iso

logger = logging.getLogger(__name__)

DEFAULT_PREDICTIONS = DATA_DIR / "pl_predictions.json"
DEFAULT_VALIDATION = DATA_DIR / "pl_validation.json"
SCHEMA_VERSION = 1
FORM_LENGTH = 5

_cache = {"path": None, "mtime": None, "data": None}


def load_predictions(path=DEFAULT_PREDICTIONS):
    """The predictions file, re-read when it changes; None when it is missing or unreadable."""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    if _cache["path"] == str(path) and _cache["mtime"] == mtime:
        return _cache["data"]
    try:
        data = read_json(path)
    except (OSError, ValueError) as exc:
        logger.warning("Could not read %s: %s", path, exc)
        return None
    if not isinstance(data, dict) or not isinstance(data.get("fixtures"), list):
        return None
    _cache.update(path=str(path), mtime=mtime, data=data)
    return data


def rounded(value, digits=4):
    return round(value, digits) if isinstance(value, float) else value


# --------------------------------------------------------------------------- #
# Reading the project's data
# --------------------------------------------------------------------------- #
def team_directory(pl):
    """{football-data id: {key, name, short, tla, crest}} and {name key: id} for the 20 clubs."""
    by_id, by_key = {}, {}
    for tid, entry in pl["teams"].items():
        profile = entry.get("profile") or {}
        key = normalise_team_name(profile.get("name"))
        by_id[int(tid)] = {"key": key, "name": profile.get("name"), "short": profile.get("short_name") or profile.get("name"),
                           "tla": profile.get("tla"), "crest": profile.get("crest")}
        by_key[key] = int(tid)
        by_key.setdefault(normalise_team_name(profile.get("short_name")), int(tid))
    return by_id, by_key


def merge_current_season(archive, history_results, id_to_key, today=None):
    """Every finished match plus the fixtures still to play.

    OpenFootball can lag by a few days, so results the project already has (football-data.org, stored in the
    history file) are laid over the current season; where both have a result the project's own wins.
    Returns (played matches, remaining fixtures, number of results OpenFootball did not have yet).
    """
    label = season_label(current_season_start(today))
    season = {(m["home"], m["away"]): m for m in archive.seasons.get(label, [])}
    added = 0
    for r in (history_results or {}).values():
        home, away = id_to_key.get(r.get("home")), id_to_key.get(r.get("away"))
        if not home or not away or r.get("home_goals") is None:
            continue
        existing = season.get((home, away))
        if existing is None or existing["hg"] is None:
            added += 1
        season[(home, away)] = {"date": r["date"], "matchday": r.get("matchday") or (existing or {}).get("matchday"),
                                "home": home, "away": away, "hg": r["home_goals"], "ag": r["away_goals"]}
    older = [m for lab, ms in archive.seasons.items() if lab != label for m in ms if m["hg"] is not None]
    played = older + [m for m in season.values() if m["hg"] is not None]
    fixtures = sorted((m for m in season.values() if m["hg"] is None), key=lambda m: (m["date"], m["home"]))
    return played, fixtures, added


def player_inputs(pl, history, by_id):
    """Chronological match lines for the player tracker, plus position and name lookups.

    Returns (matches, positions {pitch player id: group}, fd_to_pitch {football-data player id: pitch id}).
    """
    pitch_team = {}
    positions, fd_to_pitch = {}, {}
    for tid, entry in pl["teams"].items():
        pitch_id = (entry.get("sources") or {}).get("pitchapi_team_id")
        if pitch_id:
            pitch_team[pitch_id] = int(tid)
        for player in (entry.get("profile") or {}).get("squad") or []:
            pid = (player.get("pitchapi") or {}).get("pitchapi_id")
            if pid:
                fd_to_pitch[player["id"]] = pid
                group = pl_data.POSITION_GROUPS.get(player.get("position"))
                if group in pp.GROUPS:
                    positions[pid] = group
    matches = []
    for record in (history.get("pitch_matches") or {}).values():
        event = record.get("event") or {}
        home, away = pitch_team.get(event.get("match_hometeam_id")), pitch_team.get(event.get("match_awayteam_id"))
        if home is None or away is None:
            continue
        teams = {home: {}, away: {}}
        for pid, line in (record.get("players") or {}).items():
            team = pitch_team.get(line.get("t"))
            if team in teams:
                teams[team][pid] = line
        matches.append({"date": event.get("match_date"), "id": event.get("match_id"), "home": home, "away": away, "teams": teams})
    matches.sort(key=lambda m: (m["date"], m["id"]))
    return matches, positions, fd_to_pitch


def make_context(model, by_id):
    """``context(match, team)`` for the player model, as known that day: (opponent defence rating,
    venue goal factor, expected goal advantage of the player's club)."""
    fits = {}

    def context(match, team):
        day = to_ordinal(match["date"])
        fit = fits.get(day) or fits.setdefault(day, model.fit(day))
        opp = match["away"] if team == match["home"] else match["home"]
        key = by_id[opp]["key"]
        average = (fit.mu_home + fit.mu_away) / 2
        att, dfn = fit.rating(key, model.is_newcomer(key, day))
        own_key = by_id[team]["key"]
        own_att, own_def = fit.rating(own_key, model.is_newcomer(own_key, day))
        at_home = team == match["home"]
        own_goals = (fit.mu_home if at_home else fit.mu_away) * own_att * dfn
        their_goals = (fit.mu_away if at_home else fit.mu_home) * att * own_def
        return dfn, (fit.mu_home if at_home else fit.mu_away) / average, own_goals - their_goals

    return context


# --------------------------------------------------------------------------- #
# Insights
# --------------------------------------------------------------------------- #
def _letters(results):
    return "".join(r["result"] for r in results)


def team_form(model, key, as_of, names, count=FORM_LENGTH):
    """Last matches of one club: list of dicts, newest first, plus totals."""
    rows = []
    for day, home, away, hg, ag in model.recent(key, as_of, count):
        is_home = home == key
        gf, ga = (hg, ag) if is_home else (ag, hg)
        opp = away if is_home else home
        rows.append({"date": day, "opponent": names.get(opp, opp.title()), "home": is_home, "gf": gf, "ga": ga,
                     "result": "W" if gf > ga else "D" if gf == ga else "L"})
    points = sum(3 if r["result"] == "W" else 1 if r["result"] == "D" else 0 for r in rows)
    return {"matches": rows, "points": points, "ppg": round(points / len(rows), 2) if rows else None,
            "gf": sum(r["gf"] for r in rows), "ga": sum(r["ga"] for r in rows)}


def _streak(rows, wanted):
    count = 0
    for r in rows:
        if r["result"] in wanted:
            count += 1
        else:
            break
    return count


def _plural(n, word, plural=None):
    return f"{n} {word if n == 1 else plural or word + 's'}"


def _record_words(rows):
    letters = _letters(rows)
    return (f"{_plural(letters.count('W'), 'win')}, {_plural(letters.count('D'), 'draw')} and "
            f"{_plural(letters.count('L'), 'defeat')} in their last {len(rows)}")


def match_insights(home, away, pred, summary, forms, season_games):
    """Plain-language observations behind one forecast. ``home`` / ``away`` are display names."""
    p, xg = pred["probs"], pred["xg"]
    notes = []
    fav, p_fav, other = (home, p["home"], away) if p["home"] >= p["away"] else (away, p["away"], home)
    gap = abs(p["home"] - p["away"])
    if p_fav >= 0.6:
        notes.append(("favourite", f"{fav} are strong favourites ({p_fav:.0%} to win)."))
    elif gap < 0.1:
        notes.append(("close", f"Too close to call: {home} {p['home']:.0%}, draw {p['draw']:.0%}, {away} {p['away']:.0%}."))
    else:
        p_other = min(p["home"], p["away"])
        notes.append(("favourite", f"{fav} are favourites ({p_fav:.0%}); {other} {p_other:.0%}, draw {p['draw']:.0%}."))
    top = summary["top_scores"][0]
    notes.append(("goals", f"Expected goals {xg['home']:.1f} to {xg['away']:.1f}; the most likely score is {top['home']}-{top['away']} ({top['p']:.0%})."))
    if summary["over_2_5"] >= 0.6:
        notes.append(("goals", f"Goals are likely: {summary['over_2_5']:.0%} chance of 3 or more."))
    elif summary["over_2_5"] <= 0.4:
        notes.append(("goals", f"A low-scoring game is more likely than not: only {summary['over_2_5']:.0%} chance of 3 or more goals."))
    for name, form in ((home, forms["home"]), (away, forms["away"])):
        rows = form["matches"]
        if len(rows) < 3:
            continue
        unbeaten, winless = _streak(rows, "WD"), _streak(rows, "DL")
        word = (f"unbeaten in their last {unbeaten}" if unbeaten >= 4 else f"without a win in their last {winless}" if winless >= 4
                else _record_words(rows))
        notes.append(("form", f"{name}: {word} ({form['gf']} scored, {form['ga']} conceded)."))
    h2h = pred.get("h2h")
    if h2h and h2h["n"] >= 3:
        rec = h2h["record"]
        notes.append(("h2h", f"Last {h2h['n']} meetings: {home} {rec['home_wins']} wins, {rec['draws']} draws, {away} {rec['away_wins']} wins."))
    for side, name in (("home", home), ("away", away)):
        if pred["newcomer"][side]:
            notes.append(("note", f"{name} came up this season, so their strength leans on how earlier promoted clubs fared."))
    if season_games and min(season_games) < 5:
        notes.append(("note", f"Only {min(season_games)} league games played by one side this season, so the forecast relies mostly on earlier seasons."))
    return [{"kind": kind, "text": text} for kind, text in notes]


# --------------------------------------------------------------------------- #
# The build
# --------------------------------------------------------------------------- #
def build_predictions(pl, history, archive, params=None, player_params=None, validation=None, today=None, now=None):
    """Compute the whole predictions document. ``today`` and ``now`` exist so tests can pin the clock."""
    params = {**DEFAULTS, **(params or {})}
    player_params = {**pp.PARAMS, **(player_params or {})}
    today = today or date.today()
    by_id, by_key = team_directory(pl)
    id_to_key = {tid: info["key"] for tid, info in by_id.items()}
    names = {info["key"]: info["short"] for info in by_id.values()}

    played, fixtures, added = merge_current_season(archive, (history or {}).get("results"), id_to_key, today)
    model = MatchModel(played, params)
    last_day = max((to_ordinal(m["date"]) for m in played), default=to_ordinal(today))
    as_of = max(to_ordinal(today), last_day + 1)           # everything known is before this day
    fit = model.fit(as_of)

    season_games = {key: sum(1 for m in played if m["date"] >= f"{current_season_start(today)}-08-01" and key in (m["home"], m["away"]))
                    for key in id_to_key.values()}
    forms = {key: team_form(model, key, as_of, names) for key in id_to_key.values()}

    out_fixtures, unmatched = [], []
    for fx in fixtures:
        home_id, away_id = by_key.get(fx["home"]), by_key.get(fx["away"])
        if home_id is None or away_id is None:
            unmatched.append(f"{fx['home']} v {fx['away']}")
            continue
        pred = model.predict(fx["home"], fx["away"], as_of, fit=fit, params={"with_h2h_detail": True})
        summary = scoreline_summary(pred["grid"])
        home_name, away_name = names[fx["home"]], names[fx["away"]]
        meetings = [{**m, "home_name": names.get(m["home"], m["home"].title()), "away_name": names.get(m["away"], m["away"].title())}
                    for m in pred["h2h"]["meetings"][:5]]
        out_fixtures.append({
            "id": f"{fx['date']}-{home_id}-{away_id}", "date": fx["date"], "matchday": fx.get("matchday"),
            "awaiting_result": fx["date"] < today.isoformat(),   # dated before the build and no result in either source yet
            "home_id": home_id, "away_id": away_id,
            "probs": {k: rounded(v) for k, v in pred["probs"].items()},
            "xg": {k: rounded(v, 2) for k, v in pred["xg"].items()},
            "top_scores": [{**s, "p": rounded(s["p"])} for s in summary["top_scores"]],
            "over_2_5": rounded(summary["over_2_5"]), "btts": rounded(summary["btts"]),
            "clean_sheet": {k: rounded(v) for k, v in summary["clean_sheet"].items()},
            "h2h": {"n": pred["h2h"]["n"], "record": pred["h2h"]["record"], "meetings": meetings},
            "insights": match_insights(home_name, away_name, pred, summary,
                                       {"home": forms[fx["home"]], "away": forms[fx["away"]]},
                                       [season_games[fx["home"]], season_games[fx["away"]]]),
            "newcomer": pred["newcomer"],
        })

    strength = {}
    keys = list(id_to_key.values())
    attack_rank = {k: i + 1 for i, k in enumerate(sorted(keys, key=lambda k: -fit.rating(k, model.is_newcomer(k, as_of))[0]))}
    defence_rank = {k: i + 1 for i, k in enumerate(sorted(keys, key=lambda k: fit.rating(k, model.is_newcomer(k, as_of))[1]))}
    for tid, info in by_id.items():
        att, dfn = fit.rating(info["key"], model.is_newcomer(info["key"], as_of))
        strength[str(tid)] = {"attack": rounded(att, 3), "defence": rounded(dfn, 3),
                              "attack_rank": attack_rank[info["key"]], "defence_rank": defence_rank[info["key"]],
                              "newcomer": model.is_newcomer(info["key"], as_of), "games_this_season": season_games[info["key"]]}

    teams = {str(tid): {"form": forms[info["key"]], "strength": strength[str(tid)]} for tid, info in by_id.items()}
    players, player_report = build_player_outlooks(pl, history, by_id, [f for f in out_fixtures if not f["awaiting_result"]], model, fit, player_params)

    return {
        "meta": {
            "schema_version": SCHEMA_VERSION,
            "generated_at": (now or utc_now_iso()),
            "as_of": date.fromordinal(as_of).isoformat(),
            "season": season_label(current_season_start(today)),
            "model": {"match": {k: params[k] for k in DEFAULTS}, "player": player_params},
            "sources": {
                "match_history": {"name": "OpenFootball England", "url": "https://github.com/openfootball/england", "licence": "CC0",
                                  "seasons": len(archive.seasons), "finished_matches_used": len(played)},
                "current_results": "football-data.org results from pl_history.json, laid over OpenFootball (%d not yet in OpenFootball)" % added,
                "players": "PitchAPI per-match player lines in pl_history.json (appearances, minutes, goals, assists, xG, xA, ratings)",
            },
            "report": {"unmatched_fixtures": unmatched, "players": player_report},
            "validation": summarise_validation(validation),
            "not_available": [
                "Bookmaker odds (no free open source): the forecasts cannot be compared with the market.",
                "Injuries, suspensions and line-ups before kick-off: availability is guessed from recent minutes.",
                "Player data before this season: PitchAPI lines exist only for matches in pl_history.json.",
                "Assists and ratings for earlier seasons: OpenFootball has results only.",
            ],
        },
        "fixtures": out_fixtures,
        "teams": teams,
        "players": players,
    }


def build_player_outlooks(pl, history, by_id, fixtures, model, fit, player_params):
    """Next-match outlook for every squad player: {team id: {player id: {...}}} and a short report."""
    matches, positions, fd_to_pitch = player_inputs(pl, history or {}, by_id)
    tracker = pp.PlayerTracker(positions, player_params)
    for match in matches:
        tracker.observe(match)
    next_fixture = {}
    for fx in fixtures:
        next_fixture.setdefault(fx["home_id"], fx)
        next_fixture.setdefault(fx["away_id"], fx)
    average = (fit.mu_home + fit.mu_away) / 2
    out, without_data = {}, 0
    for tid, entry in pl["teams"].items():
        tid = int(tid)
        fx = next_fixture.get(tid)
        if fx is None:
            continue
        is_home = fx["home_id"] == tid
        opp = fx["away_id"] if is_home else fx["home_id"]
        opp_key = by_id[opp]["key"]
        opp_defence = fit.rating(opp_key, model.is_newcomer(opp_key, fit.as_of))[1]
        venue = (fit.mu_home if is_home else fit.mu_away) / average
        edge = fx["xg"]["home" if is_home else "away"] - fx["xg"]["away" if is_home else "home"]
        team_players = {}
        for player in (entry.get("profile") or {}).get("squad") or []:
            pid = fd_to_pitch.get(player["id"])
            pred = tracker.predict(pid, opp_defence, venue, edge) if pid else None
            if pred is None:
                without_data += 1
                continue
            last = tracker.last_game(pid)
            team_players[str(player["id"])] = {
                **{k: rounded(v, 3) if isinstance(v, float) else v for k, v in pred.items() if k != "group"},
                "next": {"fixture_id": fx["id"], "date": fx["date"], "opponent_id": opp, "home": is_home},
                "last": {"minutes": last[0], "goals": last[1], "assists": last[2], "rating": last[5], "shots": last[6]} if last else None,
            }
        out[str(tid)] = team_players
    return out, {"players_predicted": sum(len(v) for v in out.values()), "squad_players_without_match_data": without_data,
                 "matches_learned": len(matches)}


def summarise_validation(validation):
    """The few numbers the UI shows about how the match model did on history (full detail stays in pl_validation.json)."""
    team = (validation or {}).get("team_model")
    if not team:
        return None
    model, base = team["scores"]["model"], team["scores"]["constant_home_draw_away_rates"]
    return {"generated_at": validation.get("generated_at"), "test_seasons": [team["test_seasons"][0], team["test_seasons"][-1]],
            "matches": team["matches"], "accuracy": model["accuracy"], "baseline_accuracy": base["accuracy"],
            "rps": model["rps"], "baseline_rps": base["rps"], "log_loss": model["log_loss"]}


# --------------------------------------------------------------------------- #
# Helpers for the pages (read the built file, no modelling)
# --------------------------------------------------------------------------- #
def percentages(probs):
    """Home / draw / away as whole percentages that always add up to 100 (largest remainder)."""
    raw = {k: probs[k] * 100 for k in ("home", "draw", "away")}
    out = {k: int(v) for k, v in raw.items()}
    for k in sorted(raw, key=lambda k: raw[k] - out[k], reverse=True)[:100 - sum(out.values())]:
        out[k] += 1
    return out


def upcoming(doc, team_id=None):
    """Fixtures that have not been played and are not overdue, soonest first (optionally one club's)."""
    return [f for f in (doc or {}).get("fixtures", [])
            if not f.get("awaiting_result") and (team_id is None or team_id in (f["home_id"], f["away_id"]))]


def awaiting_result(doc, team_id):
    return [f for f in (doc or {}).get("fixtures", []) if f.get("awaiting_result") and team_id in (f["home_id"], f["away_id"])]


def next_matchday(doc):
    """Every upcoming fixture of the lowest matchday that still has one."""
    fixtures = upcoming(doc)
    days = [f["matchday"] for f in fixtures if f.get("matchday")]
    return [f for f in fixtures if f.get("matchday") == min(days)] if days else fixtures[:10]


def decorate_fixture(fixture, teams, team_id=None):
    """The fixture plus club details, whole percentages and (given ``team_id``) that club's point of view."""
    def club(club_id):
        return teams.get(club_id) or {"id": club_id, "name": "Unknown club", "shortName": None, "tla": None, "crest": None}

    out = {**fixture, "home": club(fixture["home_id"]), "away": club(fixture["away_id"]),
           "pct": percentages(fixture["probs"]), "score": fixture["top_scores"][0]}
    if team_id is not None:
        at_home = fixture["home_id"] == team_id
        out["at_home"] = at_home
        out["opponent"] = out["away" if at_home else "home"]
        out["own_pct"] = {"win": out["pct"]["home" if at_home else "away"], "draw": out["pct"]["draw"],
                          "loss": out["pct"]["away" if at_home else "home"]}
        out["own_xg"] = fixture["xg"]["home" if at_home else "away"]
        out["opp_xg"] = fixture["xg"]["away" if at_home else "home"]
    return out


def player_outlook(doc, team_id, player_id):
    """The stored next-match outlook of one player, or None."""
    return ((doc or {}).get("players", {}).get(str(team_id)) or {}).get(str(player_id))
