"""Full pages that widgets link to (they open in a new tab).

Same data file as the dashboard (``data/pl_teams.json``), no API calls.
Each page is a pair: ``build_<name>_context(team_id)`` returning ``(context, error)``
and a thin view, so the logic is testable without a request.
"""
import logging
from datetime import date

from django.shortcuts import render

from . import dashboard

logger = logging.getLogger(__name__)

_RATING_MIN = 1  # apifootball sends 0 / empty for "no rating"


def _shell(team_id, data=None):
    """What every club page needs: the data file, the club's entry, a small team dict and the sidebar table."""
    data = data if data is not None else dashboard.load_pl_data()
    if data is None:
        return None, None, None, "missing_data"
    entry = data["teams"].get(str(team_id))
    if not entry:
        return None, None, None, "That club isn't in the Premier League data file."
    profile = entry.get("profile") or {}
    team = {
        "id": profile.get("id", team_id),
        "name": profile.get("name"),
        "shortName": profile.get("short_name"),
        "tla": profile.get("tla"),
        "crest": profile.get("crest"),
    }
    return data, entry, team, None


def squad_rows(squad):
    """One flat record per player, with every number the squad page shows."""
    rows = []
    for player in squad or []:
        raw_position = player.get("position") or "Unknown"
        group = dashboard.POSITION_GROUPS.get(raw_position, raw_position)
        extra = player.get("apifootball") or {}
        rating = extra.get("rating")
        shirt = player.get("shirt_number") if player.get("shirt_number") is not None else extra.get("shirt_number")
        rows.append({
            "id": player.get("id"),
            "name": player.get("name") or "Unknown player",
            "position": raw_position,
            "group": group,
            "nationality": player.get("nationality") or None,
            "age": dashboard._age(player.get("date_of_birth")),
            "date_of_birth": (player.get("date_of_birth") or "")[:10] or None,
            "shirt": shirt,
            "has_stats": bool(extra),
            "appearances": extra.get("appearances"),
            "goals": extra.get("goals"),
            "assists": extra.get("assists"),
            "yellow_cards": extra.get("yellow_cards"),
            "red_cards": extra.get("red_cards"),
            "rating": rating if isinstance(rating, (int, float)) and rating >= _RATING_MIN else None,
            "injured": bool(extra.get("injured")),
            "image": extra.get("image"),
        })
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
    data, entry, team, error = _shell(team_id, data)
    if error:
        return None, error
    rows = squad_rows((entry.get("profile") or {}).get("squad"))
    return {
        "team": team,
        "team_id": team_id,
        "table": dashboard._table_rows(data.get("standings")),
        "club_link": "ufd_app:squad",  # the sidebar table stays on this page type when you pick another club
        "players": rows,
        "summary": squad_summary(rows),
        "data_updated": (data.get("meta") or {}).get("generated_at"),
        "page_title": f"{team.get('shortName') or team.get('name') or 'Club'} squad",
    }, None


def squad_view(request, team_id):
    try:
        context, error = build_squad_context(team_id)
    except Exception:  # keep the page usable whatever the data file contains
        logger.exception("Squad page failed for team %s", team_id)
        context, error = None, "Something went wrong while building the squad page."
    if error:
        return dashboard._no_data_page(request, error)
    return render(request, "squad.html", context)


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
    data, entry, team, error = _shell(team_id, data)
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
    played = stats.get("played") or 0
    record = None
    if played:
        record = {
            "played": played, "won": stats.get("won", 0), "draw": stats.get("draw", 0), "lost": stats.get("lost", 0),
            "points": stats.get("points", 0), "goals_for": stats.get("goals_for", 0), "goals_against": stats.get("goals_against", 0),
            "win_rate": round(100 * (stats.get("won") or 0) / played),
            "avg_points": f"{stats['avg_points']:.2f}" if isinstance(stats.get("avg_points"), (int, float)) else None,
            "position": stats.get("position"),
        }

    assistants = [{"name": a.get("name"), "role": a.get("role")} for a in extras.get("assistant_coaches", [])
                  if isinstance(a, dict) and a.get("name")]
    return {
        "team": team, "team_id": team_id,
        "table": dashboard._table_rows(data.get("standings")),
        "club_link": "ufd_app:manager",
        "manager": manager, "record": record, "assistants": assistants,
        "formations": _formation_usage(entry),
        "data_updated": (data.get("meta") or {}).get("generated_at"),
        "page_title": f"{team.get('shortName') or team.get('name') or 'Club'} manager",
    }, None


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
    data, entry, team, error = _shell(team_id, data)
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
    return {
        "team": team, "team_id": team_id,
        "table": dashboard._table_rows(data.get("standings")),
        "club_link": "ufd_app:formation",
        "options": options, "selected": selected, "dock": dock,
        "matches_counted": sum(i["count"] for i in options),
        "data_updated": (data.get("meta") or {}).get("generated_at"),
        "page_title": f"{team.get('shortName') or team.get('name') or 'Club'} formations",
    }, None


def formation_view(request, team_id):
    return _render_page(request, team_id, build_formation_context, "formation.html", "formation")


# --------------------------------------------------------------------------- #
# Player page
# --------------------------------------------------------------------------- #
# (heading, [(key in the player's apifootball numbers, label, "pct" for percentages)])
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


def _whole(value):
    return int(value) if isinstance(value, float) and value == int(value) else value


def build_player_context(team_id, player_id, data=None):
    data, entry, team, error = _shell(team_id, data)
    if error:
        return None, error
    squad = (entry.get("profile") or {}).get("squad") or []
    player = next((p for p in squad if p.get("id") == player_id), None)
    if player is None:
        return None, "That player isn't in this club's squad data."
    row = next(r for r in squad_rows([player]))
    extra = player.get("apifootball") or {}
    numbers = extra.get("extra") or {}

    sections = []
    for heading, fields in PLAYER_STAT_SECTIONS:
        if heading == "Goalkeeping" and row["group"] != "Goalkeeper":
            continue
        items = []
        for field in fields:
            value = numbers.get(field[0])
            # apifootball sends 0 for numbers it doesn't track, so a 0 is only shown next to a real figure
            if value is None or (value == 0 and not any(numbers.get(k[0]) for k in fields)):
                continue
            items.append({"label": field[1], "value": f"{_whole(value)}%" if len(field) > 2 else _whole(value)})
        if items:
            sections.append({"heading": heading, "items": items})

    # Penalties are always listed when apifootball sends them: a 0 here is a real "none taken", not missing data
    pens = [(label, numbers.get(key)) for key, label in (("pen_scored", "Penalties scored"), ("pen_missed", "Penalties missed"),
                                                        ("pen_won", "Penalties won"), ("pen_committed", "Penalties conceded"))]
    if any(value is not None for _, value in pens):
        sections.append({"heading": "Penalties", "items": [{"label": l, "value": _whole(v)} for l, v in pens if v is not None]})

    first = next((p for p in squad if p.get("id") == player_id), {})
    team_name = team.get("shortName") or team.get("name") or "Club"
    return {
        "team": team, "team_id": team_id,
        "table": dashboard._table_rows(data.get("standings")),
        "club_link": "ufd_app:squad",
        "p": {**row, "full_name": extra.get("full_name") or row["name"], "captain": bool(extra.get("captain")),
              "minutes": _whole(numbers.get("minutes")) or None, "born": row["date_of_birth"],
              "birth_year": (row["date_of_birth"] or "")[:4] or None},
        "sections": sections,
        "more": [item for section in sections for item in section["items"]],
        "has_stats": row["has_stats"],
        "data_updated": (data.get("meta") or {}).get("generated_at"),
        "page_title": f"{row['name']} · {team_name}",
    }, None


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
