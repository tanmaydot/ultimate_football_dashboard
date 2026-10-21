"""Full pages that widgets link to (they open in a new tab).

Same data file as the dashboard (``data/pl_teams.json``), no API calls.
Each page is a pair: ``build_<name>_context(team_id)`` returning ``(context, error)``
and a thin view, so the logic is testable without a request.
"""
import logging
from datetime import date

from django.shortcuts import render

from . import dashboard, predictions
from .util import whole

logger = logging.getLogger(__name__)

def squad_rows(squad):
    """One flat record per player, with every number the squad page shows."""
    rows = [dashboard.player_row(player) for player in squad or []]
    order = {name: i for i, name in enumerate(dashboard.GROUP_ORDER)}
    rows.sort(key=lambda r: (order.get(r["group"], len(order)), r["name"]))
    return rows


def squad_summary(rows):
    ages = [r["age"] for r in rows if r["age"] is not None]
    return {
        "players": len(rows),
        "average_age": round(sum(ages) / len(ages), 1) if ages else None,
        "nationalities": len({r["nationality"] for r in rows if r["nationality"]}),
        "injured": sum(1 for r in rows if r["injured"]),
        "without_stats": sum(1 for r in rows if not r["has_stats"]),
        "groups": [(name, sum(1 for r in rows if r["group"] == name))
                   for name in dashboard.GROUP_ORDER if any(r["group"] == name for r in rows)],
    }


def build_squad_context(team_id, data=None):
    data, entry, team, error = dashboard.load_club(team_id, data)
    if error:
        return None, error
    rows = squad_rows((entry.get("profile") or {}).get("squad"))
    doc = predictions.load_predictions()
    for row in rows:
        row["outlook"] = predictions.player_outlook(doc, team_id, row["id"])
    return {**dashboard.page_base(data, team, team_id, "ufd_app:squad", "squad"),
            "players": rows, "summary": squad_summary(rows), "has_outlook": any(r["outlook"] for r in rows)}, None


def squad_view(request, team_id):
    return _render_page(request, team_id, build_squad_context, "squad.html", "squad")


# --------------------------------------------------------------------------- #
# Manager & coaches
# --------------------------------------------------------------------------- #
def _months_until(year_month):
    """Whole months from today until a 'YYYY-MM' (or 'YYYY-MM-DD') date; None if unparseable. Negative once passed."""
    try:
        year, month = int(year_month[:4]), int(year_month[5:7])
    except (TypeError, ValueError):
        return None
    today = date.today()
    return (year - today.year) * 12 + (month - today.month)


def _contract_label(months):
    if months is None:
        return None
    if months < 0:
        return "Contract has ended"
    if months == 0:
        return "Contract ends this month"
    years, rest = divmod(months, 12)
    parts = ([f"{years} year{'s' if years != 1 else ''}"] if years else []) + ([f"{rest} month{'s' if rest != 1 else ''}"] if rest else [])
    return " ".join(parts) + " left"


def build_manager_context(team_id, data=None, extras_by_team=None):
    data, entry, team, error = dashboard.load_club(team_id, data)
    if error:
        return None, error
    profile = entry.get("profile") or {}
    extras = (extras_by_team if extras_by_team is not None else dashboard.load_club_extras()).get(str(team_id), {})

    coach = profile.get("coach") or None
    manager = None
    if coach and coach.get("name"):
        contract = coach.get("contract") or {}
        months = _months_until(contract.get("until"))
        manager = {
            "name": coach["name"],
            "nationality": coach.get("nationality"),
            "age": dashboard._age(coach.get("date_of_birth")),
            "date_of_birth": (coach.get("date_of_birth") or "")[:10] or None,
            "contract_start": contract.get("start"),
            "contract_until": contract.get("until"),
            "contract_left": _contract_label(months),
        }

    stats = entry.get("stats") or {}
    season = dashboard._season_stats(stats)
    record = None
    if season:
        avg_points = season["avg_points"] if isinstance(stats.get("avg_points"), (int, float)) else None
        record = {**season, "win_rate": season["win_pct"], "avg_points": avg_points, "position": stats.get("position")}

    return {**dashboard.page_base(data, team, team_id, "ufd_app:manager", "manager"),
            "manager": manager, "record": record, "assistants": dashboard.assistant_coaches(extras),
            "formations": _formation_usage(entry)}, None


def manager_view(request, team_id):
    return _render_page(request, team_id, build_manager_context, "manager.html", "manager")


# --------------------------------------------------------------------------- #
# Formation
# --------------------------------------------------------------------------- #
def _formation_usage(entry):
    """Every formation the club has played this season: [{formation, count, share, last_date, starting_xi}], most used first."""
    info = entry.get("formation") or {}
    items = info.get("formations")
    if not items:  # data files built before per-formation details existed only have counts
        items = [{"formation": f, "count": n, "last_date": None, "starting_xi": None} for f, n in (info.get("usage") or {}).items()]
        items.sort(key=lambda i: -i["count"])
    total = sum(i["count"] for i in items) or 1
    return [{**i, "share": round(100 * i["count"] / total), "last_date": (i.get("last_date") or "")[:10] or None} for i in items]


def build_formation_context(team_id, data=None):
    data, entry, team, error = dashboard.load_club(team_id, data)
    if error:
        return None, error
    squad = (entry.get("profile") or {}).get("squad") or []
    most_used = (entry.get("formation") or {}).get("default")

    options = []
    for item in _formation_usage(entry):
        pitch = dashboard.build_formation(item["formation"], item.get("starting_xi"), squad)
        if pitch:
            options.append({**item, "pitch": pitch, "is_most_used": item["formation"] == most_used})
    # open on the most used formation (the list is already sorted by how often each was played)
    selected = next((i["formation"] for i in options if i["is_most_used"]), options[0]["formation"] if options else None)
    if options and not any(i["is_most_used"] for i in options):
        options[0]["is_most_used"] = True
    dock = [{"id": r["id"], "name": dashboard._short_name(r["name"]), "full": r["name"], "shirt": r["shirt"],
             "group": r["group"], "position": r["position"], "injured": r["injured"]}
            for r in squad_rows(squad) if r["id"] is not None]
    return {**dashboard.page_base(data, team, team_id, "ufd_app:formation", "formations"),
            "options": options, "selected": selected, "dock": dock,
            "matches_counted": sum(i["count"] for i in options)}, None


def formation_view(request, team_id):
    return _render_page(request, team_id, build_formation_context, "formation.html", "formation")


# --------------------------------------------------------------------------- #
# Player page
# --------------------------------------------------------------------------- #
# (heading, [(key in the player's season numbers, label, "pct" for percentages)])
PLAYER_STAT_SECTIONS = [
    ("Attacking", [("shots", "Shots"), ("dribble_attempts", "Dribble attempts"), ("dribbles", "Dribbles completed"),
                   ("woodwork", "Hit the woodwork")]),
    ("Passing", [("passes", "Passes"), ("pass_accuracy", "Pass accuracy", "pct"), ("key_passes", "Key passes"),
                 ("crosses", "Crosses")]),
    ("Defending", [("tackles", "Tackles"), ("interceptions", "Interceptions"), ("clearances", "Clearances"),
                   ("blocks", "Blocks"), ("duels", "Duels"), ("duels_won", "Duels won"),
                   ("fouls", "Fouls committed"), ("dispossessed", "Dispossessed")]),
    ("Goalkeeping", [("saves", "Saves"), ("inside_box_saves", "Saves from inside the box"),
                     ("goals_conceded", "Goals conceded")]),
]


def build_player_context(team_id, player_id, data=None):
    data, entry, team, error = dashboard.load_club(team_id, data)
    if error:
        return None, error
    squad = (entry.get("profile") or {}).get("squad") or []
    player = next((p for p in squad if p.get("id") == player_id), None)
    if player is None:
        return None, "That player isn't in this club's squad data."
    row = next(r for r in squad_rows([player]))
    extra = dashboard.pl_data.player_numbers(player)
    numbers = extra.get("extra") or {}

    sections = []
    for heading, fields in PLAYER_STAT_SECTIONS:
        if heading == "Goalkeeping" and row["group"] != "Goalkeeper":
            continue
        items = []
        for field in fields:
            value = numbers.get(field[0])
            # a 0 can mean "not tracked", so it is only shown next to a real figure
            if value is None or (value == 0 and not any(numbers.get(k[0]) for k in fields)):
                continue
            items.append({"label": field[1], "value": f"{whole(value)}%" if len(field) > 2 else whole(value)})
        if items:
            sections.append({"heading": heading, "items": items})

    # Penalties are always listed: a 0 here is a real "none taken", not missing data
    pens = [(label, numbers.get(key)) for key, label in (("pen_scored", "Penalties scored"), ("pen_missed", "Penalties missed"),
                                                        ("pen_won", "Penalties won"), ("pen_committed", "Penalties conceded"))]
    if any(value is not None for _, value in pens):
        sections.append({"heading": "Penalties", "items": [{"label": l, "value": whole(v)} for l, v in pens if v is not None]})

    base = dashboard.page_base(data, team, team_id, "ufd_app:squad", "")
    return {
        **base,
        "p": {**row, "full_name": extra.get("full_name") or row["name"], "captain": bool(extra.get("captain")),
              "minutes": whole(numbers.get("minutes")) or None, "born": row["date_of_birth"],
              "birth_year": (row["date_of_birth"] or "")[:4] or None},
        "sections": sections,
        "more": [item for section in sections for item in section["items"]],
        "has_stats": row["has_stats"],
        "outlook": _player_outlook_card(team_id, row["id"], data),
        "page_title": f"{row['name']} · {base['page_title']}",
    }, None


def _player_outlook_card(team_id, player_id, data):
    """The next-match prediction of one player with the opponent's details, or None when there is none."""
    doc = predictions.load_predictions()
    outlook = predictions.player_outlook(doc, team_id, player_id)
    if not outlook:
        return None
    opponent = dashboard.load_club(outlook["next"]["opponent_id"], data)[2] or {}
    return {**outlook, "opponent": opponent, "pct": {k: round(outlook[f"p_{k}"] * 100) for k in ("goal", "assist", "goal_or_assist")},
            "as_of": doc["meta"]["as_of"]}


def player_view(request, team_id, player_id):
    return _render_page(request, team_id, lambda tid: build_player_context(tid, player_id), "player.html", "player")


def _render_page(request, team_id, builder, template, name):
    try:
        context, error = builder(team_id)
    except Exception:  # keep the page usable whatever the data file contains
        logger.exception("%s page failed for team %s", name, team_id)
        context, error = None, f"Something went wrong while building the {name} page."
    if error:
        return dashboard._no_data_page(request, error)
    return render(request, template, context)


# --------------------------------------------------------------------------- #
# Predictions
# --------------------------------------------------------------------------- #
UPCOMING_SHOWN = 8


def build_predictions_context(team_id, data=None, doc=None):
    data, entry, team, error = dashboard.load_club(team_id, data)
    if error:
        return None, error
    doc = doc if doc is not None else predictions.load_predictions()
    context = dashboard.page_base(data, team, team_id, "ufd_app:predictions", "predictions")
    context["doc"] = bool(doc)
    if not doc:
        return context, None
    teams = dashboard.team_lookup(data)
    decorated = [predictions.decorate_fixture(f, teams, team_id) for f in predictions.upcoming(doc, team_id)]
    rows = squad_rows((entry.get("profile") or {}).get("squad"))
    players = []
    for row in rows:
        outlook = predictions.player_outlook(doc, team_id, row["id"])
        if outlook:
            players.append({**row, "o": outlook, "pct_goal": round(outlook["p_goal"] * 100, 1),
                            "pct_assist": round(outlook["p_assist"] * 100, 1),
                            "pct_either": round(outlook["p_goal_or_assist"] * 100, 1)})
    players.sort(key=lambda p: -p["o"]["p_goal_or_assist"])
    meta = doc["meta"]
    context.update({
        "next_match": decorated[0] if decorated else None,
        "fixtures": decorated[1:UPCOMING_SHOWN],
        "awaiting": [predictions.decorate_fixture(f, teams, team_id) for f in predictions.awaiting_result(doc, team_id)],
        "league_fixtures": [predictions.decorate_fixture(f, teams) for f in predictions.next_matchday(doc)],
        "form": (doc["teams"].get(str(team_id)) or {}).get("form"),
        "strength": (doc["teams"].get(str(team_id)) or {}).get("strength"),
        "players": players,
        "players_missing": len(rows) - len(players),
        "top_scorers": sorted(players, key=lambda p: -p["o"]["p_goal"])[:3],
        "meta": meta,
        "validation": meta.get("validation"),
        "season_label": meta.get("season"),
    })
    return context, None


def predictions_view(request, team_id):
    return _render_page(request, team_id, build_predictions_context, "predictions.html", "predictions")
