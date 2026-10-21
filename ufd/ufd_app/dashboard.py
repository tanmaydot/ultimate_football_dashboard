"""Premier League team dashboard (movable / removable widgets).

The page is filled from ``data/pl_teams.json``, built by
``python manage.py build_pl_data`` (see ``pl_data.py``). The dashboard makes no
API calls itself, so it is fast and can't hit a rate limit.

Two extras files sit on top of that, merged per club:

* ``data/club_extras.auto.json`` - generated quarterly (first day of Jan/Apr/Jul/Oct) by
  ``python manage.py refresh_club_extras`` (stadium + honours from the
  Transfermarkt API / Wikidata). Do not edit by hand.
* ``data/club_extras.json`` - hand-kept, for what no free source has (revenue,
  assistant coaches) and for overriding anything in the generated file or the
  default formation. It always wins.

Missing data never breaks the page: the widget shows a placeholder.
"""
import logging
import re
from datetime import date
from pathlib import Path

from django.contrib import messages
from django.shortcuts import redirect, render
from django.urls import reverse

from . import pl_data, predictions
from .util import ordinal_suffix, read_json

logger = logging.getLogger(__name__)

LEAGUE_CODE = "PL"
PL_DATA_PATH = pl_data.DEFAULT_OUTPUT
EXTRAS_PATH = Path(__file__).resolve().parent / "data" / "club_extras.json"
AUTO_EXTRAS_PATH = Path(__file__).resolve().parent / "data" / "club_extras.auto.json"

# Default layout on a 12-column grid: squad on the right, everything else on the left.
# (The league table lives in the collapsible sidebar, not in the grid.)

DEFAULT_LAYOUT = [
    {"id": "info", "title": "Team info", "icon": "fa-info-circle",
     "x": 0, "y": 0, "w": 12, "h": 5, "minW": 4, "minH": 4,
     "fit": {"h": 356}},

    {"id": "stats", "title": "Season stats", "icon": "fa-chart-bar",
     "x": 0, "y": 5, "w": 6, "h": 7, "minW": 3, "minH": 6,
     "fit": {"w": 534, "h": 524}},

    {"id": "formation", "title": "Formation", "icon": "fa-chess-board",
     "x": 6, "y": 5, "w": 6, "h": 7, "minW": 3, "minH": 6},

    {"id": "squad", "title": "Squad & manager", "icon": "fa-users",
     "x": 0, "y": 12, "w": 12, "h": 7, "minW": 4, "minH": 5,
     "fit": {"h": 556}},

    {"id": "prediction", "title": "Next match", "icon": "fa-wand-magic-sparkles",
     "x": 0, "y": 19, "w": 12, "h": 6, "minW": 4, "minH": 5},

    # Revenue and coaches remain hidden until added from the Add Widget menu.
    {"id": "revenue", "title": "Revenue", "icon": "fa-coins",
     "x": 6, "y": 21, "w": 3, "h": 5, "minW": 2, "minH": 3,
     "hidden": True},

    {"id": "coaches", "title": "Coaches & assistants",
     "icon": "fa-clipboard-list", "x": 9, "y": 21, "w": 3, "h": 4,
     "minW": 2, "minH": 3, "hidden": True},
]


# Widgets whose title opens a full page in a new tab: widget id -> URL name (each takes the club id).
PAGE_ROUTES = {"squad": "ufd_app:squad", "coaches": "ufd_app:manager", "formation": "ufd_app:formation",
               "prediction": "ufd_app:predictions"}

POSITION_GROUPS = pl_data.POSITION_GROUPS
GROUP_ORDER = ["Goalkeeper", "Defence", "Midfield", "Offence"]

_FORMATION_RE = re.compile(r"^\d(-\d){1,4}$")
_OUTFIELD_ROLES = {
    2: ["DEF", "FWD"],
    3: ["DEF", "MID", "FWD"],
    4: ["DEF", "DM", "AM", "FWD"],
    5: ["DEF", "DM", "MID", "AM", "FWD"],
}
_CURRENCY_SYMBOLS = {"GBP": "£", "EUR": "€", "USD": "$"}

_data_cache = {"path": None, "mtime": None, "data": None}


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
# Honour name (as written in club_extras) -> which trophy drawing to use in partials/trophy_art.html.
HONOUR_KINDS = {
    "english league champions": "league",
    "premier league": "league",
    "fa cup": "fa",
    "league cup": "leaguecup",
    "community shield": "shield",
    "european cup / champions league": "european",
    "uefa cup": "uefa",
    "europa league": "europa",
    "cup winners' cup": "cwc",
    "conference league": "conference",
    "inter-cities fairs cup": "fairs",
    "uefa super cup": "super",
    "club world cup": "globe",
    "intercontinental cup": "globe",
    "second tier champions": "tier2",
}


def honour_kind(name):
    """Which trophy drawing fits an honour; anything unknown gets a plain golden cup."""
    return HONOUR_KINDS.get((name or "").strip().lower(), "cup")


def load_pl_data():
    """The generated dataset, or None if it hasn't been built (or can't be read).

    Re-read automatically when the file changes, so rebuilding the data doesn't
    need a server restart.
    """
    path = Path(PL_DATA_PATH)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    if _data_cache["path"] == str(path) and _data_cache["mtime"] == mtime:
        return _data_cache["data"]
    try:
        data = read_json(path)
    except (OSError, ValueError) as exc:
        logger.warning("Could not read %s: %s", path, exc)
        return None
    if not isinstance(data, dict) or not isinstance(data.get("teams"), dict) or not data["teams"]:
        logger.warning("%s has no teams in it", path)
        return None
    _data_cache.update(path=str(path), mtime=mtime, data=data)
    return data


def _read_extras_file(path):
    """One extras file as {team_id: dict}. Keys starting with '_' are comments."""
    try:
        raw = read_json(path)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning("Could not read %s: %s", path, exc)
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(key): value
        for key, value in raw.items()
        if not str(key).startswith("_") and isinstance(value, dict)
    }


def merge_extras(auto, manual):
    """Per club: the hand-kept entry wins field by field over the generated one.

    ``stadium`` is merged key by key (so you can fix just the capacity and keep the
    generated city and year); every other field, including the ``honours`` list, is
    replaced as a whole when the hand-kept file has it.
    """
    merged = {}
    for team_id in set(auto) | set(manual):
        entry = dict(auto.get(team_id, {}))
        for key, value in manual.get(team_id, {}).items():
            if isinstance(value, dict) and isinstance(entry.get(key), dict):
                entry[key] = {**entry[key], **value}
            else:
                entry[key] = value
        merged[team_id] = entry
    return merged


def load_club_extras():
    """Generated extras with the hand-kept ones laid on top (hand-kept wins)."""
    return merge_extras(_read_extras_file(AUTO_EXTRAS_PATH), _read_extras_file(EXTRAS_PATH))


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def format_money(value, currency="EUR"):
    """12_300_000 -> '€12m', 1_450_000_000 -> '€1.5bn'."""
    symbol = _CURRENCY_SYMBOLS.get(currency, f"{currency} ")
    if value >= 1_000_000_000:
        return f"{symbol}{value / 1_000_000_000:.1f}bn"
    if value >= 1_000_000:
        return f"{symbol}{value / 1_000_000:,.0f}m"
    return f"{symbol}{value:,.0f}"


def _age(date_of_birth):
    try:
        born = date.fromisoformat((date_of_birth or "")[:10])
    except ValueError:
        return None
    today = date.today()
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))


def _short_name(full_name):
    """'Cole Palmer' -> 'Palmer', 'Virgil van Dijk' -> 'van Dijk'."""
    tokens = (full_name or "").split()
    if not tokens:
        return ""
    i = len(tokens) - 1
    while i > 0 and tokens[i - 1][:1].islower():
        i -= 1
    return " ".join(tokens[i:])


# --------------------------------------------------------------------------- #
# Widget data
# --------------------------------------------------------------------------- #
RATING_MIN = 1  # a rating of 0 / empty means "no rating"


def player_row(player):
    """One flat record per player with everything the squad widget and the squad page show."""
    raw_position = player.get("position") or "Unknown"
    extra = pl_data.player_numbers(player)
    rating = extra.get("rating")
    shirt = player.get("shirt_number")
    return {
        "id": player.get("id"),
        "name": player.get("name") or "Unknown player",
        "position": raw_position,
        "group": POSITION_GROUPS.get(raw_position, raw_position),
        "nationality": player.get("nationality") or None,
        "age": _age(player.get("date_of_birth")),
        "date_of_birth": (player.get("date_of_birth") or "")[:10] or None,
        "shirt": shirt if shirt is not None else extra.get("shirt_number"),
        "has_stats": bool(extra),
        "appearances": extra.get("appearances"),
        "goals": extra.get("goals"),
        "assists": extra.get("assists"),
        "yellow_cards": extra.get("yellow_cards"),
        "red_cards": extra.get("red_cards"),
        "rating": rating if isinstance(rating, (int, float)) and rating >= RATING_MIN else None,
        "injured": bool(extra.get("injured")),
        "image": extra.get("image"),
    }


def group_squad(squad):
    """Group the squad by Goalkeeper / Defence / Midfield / Offence."""
    groups = {name: [] for name in GROUP_ORDER}
    other = {}
    for player in squad or []:
        row = player_row(player)
        row["nationality"] = row["nationality"] or "—"
        target = groups[row["group"]] if row["group"] in groups else other.setdefault(row["group"], [])
        target.append(row)

    ordered = [(name, sorted(groups[name], key=lambda p: p["name"])) for name in GROUP_ORDER if groups[name]]
    ordered += [(name, sorted(players, key=lambda p: p["name"])) for name, players in other.items()]
    return ordered


def team_lookup(data):
    """{club id: team dict} for every club in the data file (used to name the clubs in fixture lists)."""
    return {int(tid): team_summary((entry.get("profile") or {}), tid) for tid, entry in data["teams"].items()}


def next_match_card(data, team_id):
    """The dashboard widget's content: the club's next forecast fixture, or None when no predictions exist yet."""
    doc = predictions.load_predictions()
    fixtures = predictions.upcoming(doc, team_id)
    return predictions.decorate_fixture(fixtures[0], team_lookup(data), team_id) if fixtures else None


def team_summary(profile, team_id):
    """The few club fields every page needs."""
    return {
        "id": profile.get("id", team_id),
        "name": profile.get("name"),
        "shortName": profile.get("short_name"),
        "tla": profile.get("tla"),
        "crest": profile.get("crest"),
    }


def load_club(team_id, data=None):
    """(data file, the club's entry, team dict, error). ``error`` is a user-facing message or None."""
    data = data if data is not None else load_pl_data()
    if data is None:
        return None, None, None, "missing_data"
    entry = data["teams"].get(str(team_id))
    if not entry:
        return None, None, None, "That club isn't in the Premier League data file."
    return data, entry, team_summary(entry.get("profile") or {}, team_id), None


def assistant_coaches(extras):
    """The hand-kept assistant list (the APIs hardly ever list staff)."""
    return [{"name": a.get("name"), "role": a.get("role")}
            for a in extras.get("assistant_coaches", []) if isinstance(a, dict) and a.get("name")]


def page_base(data, team, team_id, club_link, title):
    """Context keys every club page shares."""
    return {
        "team": team,
        "team_id": team_id,
        "table": _table_rows(data.get("standings")),
        "club_link": club_link,  # the sidebar table stays on this page type when you pick another club
        "data_updated": (data.get("meta") or {}).get("generated_at"),
        "page_title": f"{team.get('shortName') or team.get('name') or 'Club'} {title}".strip(),
    }


def find_player_id(name, squad):
    """Squad id of the player a lineup name ('E. Haaland', 'Erling Haaland') refers to; None when unsure."""
    tokens = pl_data._person_key(name)
    if not tokens:
        return None
    people = [(p, pl_data._person_key(p.get("name"))) for p in squad or [] if p.get("id") is not None]
    exact = [p for p, key in people if key == tokens]
    if len(exact) == 1:
        return exact[0].get("id")
    same = [(p, key) for p, key in people if key and key[-1] == tokens[-1]]
    if len(same) > 1 and len(tokens) > 1:
        same = [(p, key) for p, key in same if key[0][:1] == tokens[0][:1]]
    return same[0][0].get("id") if len(same) == 1 else None


def build_formation(label, starting_xi, squad):
    """Turn '4-3-3' into rows of slots, goalkeeper first.

    ``starting_xi`` is 11 players, goalkeeper first then defence -> attack. It
    can be player names (from the generated data) or football-data player ids
    (from club_extras.json). Without it the slots just show the role.
    """
    if not label or not _FORMATION_RE.match(str(label).strip()):
        return None
    label = str(label).strip()
    counts = [int(n) for n in label.split("-")]
    if sum(counts) != 10:
        return None

    names = ids = None
    if isinstance(starting_xi, list) and len(starting_xi) == 11:
        if all(isinstance(item, int) for item in starting_xi):
            by_id = {p.get("id"): p.get("name") for p in squad or []}
            names = [_short_name(by_id.get(pid)) or None for pid in starting_xi]
            ids = [pid if pid in by_id else None for pid in starting_xi]
        elif all(isinstance(item, str) for item in starting_xi):
            names = [_short_name(item) or None for item in starting_xi]
            ids = [find_player_id(item, squad) for item in starting_xi]

    lines = [{"role": "GK", "slots": [{"label": "GK", "name": names[0] if names else None, "id": ids[0] if ids else None, "n": 0}]}]
    index = 1
    for role, count in zip(_OUTFIELD_ROLES[len(counts)], counts):
        slots = []
        for _ in range(count):
            slots.append({"label": role, "name": names[index] if names else None, "id": ids[index] if ids else None, "n": index})
            index += 1
        # The lineup lists each row's players in the opposite order to how the pitch is drawn,
        # which put left-sided players on the right. Mirror every row.
        slots.reverse()
        lines.append({"role": role, "slots": slots})
    return {"label": label, "lines": lines, "has_names": bool(names)}


_COLOUR_HEX = {
    "white": "#ffffff", "black": "#111827", "red": "#dc2626", "blue": "#2563eb", "royal blue": "#1e4fb3",
    "sky blue": "#6cabdd", "light blue": "#6cabdd", "navy": "#1b2a4e", "navy blue": "#1b2a4e", "dark blue": "#1b2a4e",
    "claret": "#7a263a", "burgundy": "#7a263a", "maroon": "#7a1f2b", "yellow": "#f5c518", "gold": "#d4a017",
    "amber": "#f59e0b", "orange": "#f97316", "green": "#15803d", "purple": "#6d28d9", "pink": "#ec4899",
    "silver": "#c0c0c0", "grey": "#6b7280", "gray": "#6b7280", "lilac": "#a78bfa", "teal": "#0d9488",
}


def club_colours(text):
    """'Royal Blue / White' -> [{'name': 'Royal Blue', 'hex': '#1e4fb3'}, ...] (unknown names are skipped)."""
    colours = []
    for part in re.split(r"[/,&]|\band\b", text or "", flags=re.I):
        name = part.strip()
        hex_value = _COLOUR_HEX.get(name.lower())
        if name and hex_value:
            colours.append({"name": name.title(), "hex": hex_value})
    return colours


def _brand_colours(colours):
    """Two colours for the hero glow: skip white/black unless that is all the club has."""
    vivid = [c["hex"] for c in colours if c["hex"] not in ("#ffffff", "#111827")]
    pool = vivid or [c["hex"] for c in colours]
    return (pool[0], pool[1] if len(pool) > 1 else pool[0]) if pool else (None, None)


def _contract_progress(start, until):
    """Percent of a 'YYYY-MM' contract that has passed (None when the dates are unusable)."""
    try:
        a = int(start[:4]) * 12 + int(start[5:7])
        b = int(until[:4]) * 12 + int(until[5:7])
    except (TypeError, ValueError):
        return None
    today = date.today()
    now = today.year * 12 + today.month
    return max(0, min(100, round(100 * (now - a) / (b - a)))) if b > a else None


def _position_label(position, table_size):
    """1 -> '1st of 20' (None when the club isn't in the table)."""
    if not isinstance(position, int) or position < 1:
        return None
    return f"{position}{ordinal_suffix(position)}" + (f" of {table_size}" if table_size else "")


def _table_rows(standings):
    """Standings from the data file, in the row shape the template already uses."""
    return [
        {
            "position": row.get("position"),
            "team": {"id": row.get("team_id"), "crest": row.get("crest"), "tla": row.get("tla"),
                     "shortName": row.get("short_name"), "name": row.get("name")},
            "playedGames": row.get("played"),
            "goalDifference": row.get("goal_difference") or 0,
            "points": row.get("points"),
        }
        for row in standings or []
    ]


def _season_stats(stats):
    if not stats or not stats.get("played"):
        return None
    played, avg = stats["played"], stats.get("avg_points")
    won, draw, lost = (stats.get(key, 0) for key in ("won", "draw", "lost"))
    goals_for, goals_against = stats.get("goals_for", 0), stats.get("goals_against", 0)
    goals_max = max(goals_for, goals_against, 1)
    return {
        "played": played,
        "won": won,
        "draw": draw,
        "lost": lost,
        "points": stats.get("points", 0),
        "avg_points": f"{avg:.2f}" if isinstance(avg, (int, float)) else "—",
        "goals_for": goals_for,
        "goals_against": goals_against,
        "goal_difference": stats.get("goal_difference", 0),
        "clean_sheets": stats.get("clean_sheets"),
        "possession": stats.get("possession"),
        "form": [c for c in re.split(r"[,\s]+", str(stats.get("form") or "")) if c in ("W", "D", "L")][-5:],
        "win_pct": round(100 * won / played),
        "draw_pct": round(100 * draw / played),
        "loss_pct": round(100 * lost / played),
        "goals_max": goals_max,
        "gf_pct": round(100 * goals_for / goals_max),
        "ga_pct": round(100 * goals_against / goals_max),
    }


def build_dashboard_context(team_id, data=None, extras_by_team=None):
    """Return (context, error). ``error`` is a user-facing message or None."""
    data, entry, team, error = load_club(team_id, data)
    if error:
        return None, error

    all_extras = extras_by_team if extras_by_team is not None else load_club_extras()
    extras = all_extras.get(str(team_id), {})
    profile = entry.get("profile") or {}
    squad = profile.get("squad") or []
    stats = entry.get("stats") or {}
    table_rows = _table_rows(data.get("standings"))

    position = stats.get("position")
    team.update({
        "founded": profile.get("founded"),
        "clubColors": profile.get("club_colors"),
        "website": profile.get("website"),
        "address": profile.get("address"),
        "area": {"name": profile.get("country")},
        "position": _position_label(position, len(table_rows)),
        "rank": position if isinstance(position, int) else None,
        "table_size": len(table_rows) or None,
        "rank_suffix": ordinal_suffix(position),
        "points": stats.get("points"),
        "colours": club_colours(profile.get("club_colors")),
    })
    team["brand_a"], team["brand_b"] = _brand_colours(team["colours"])

    # Revenue / market value (revenue is manual: neither API has it)
    revenue = None
    raw_revenue = extras.get("revenue")
    if isinstance(raw_revenue, dict) and isinstance(raw_revenue.get("amount_m"), (int, float)):
        revenue = {
            "display": format_money(raw_revenue["amount_m"] * 1_000_000, raw_revenue.get("currency", "GBP")),
            "season": raw_revenue.get("season"),
            "source": raw_revenue.get("source"),
        }

    # Stadium: football-data gives the name; capacity, city and opening year come from the club_extras files
    venue = profile.get("venue") or {}
    stadium_extras = extras.get("stadium") if isinstance(extras.get("stadium"), dict) else {}
    capacity = stadium_extras.get("capacity", venue.get("capacity"))
    stadium = {
        "name": stadium_extras.get("name") or venue.get("name"),
        "capacity": f"{capacity:,}" if isinstance(capacity, int) else None,
        "city": stadium_extras.get("city") or venue.get("city"),
        "opened": stadium_extras.get("opened"),
    }
    # How big is the ground next to the rest of the league?
    # (every club's capacity, from the extras files)
    def _capacity_of(tid, team_entry):
        club_stadium = (all_extras.get(str(tid), {}) or {}).get("stadium")
        cap = club_stadium.get("capacity") if isinstance(club_stadium, dict) else None
        return cap if cap is not None else ((team_entry.get("profile") or {}).get("venue") or {}).get("capacity")

    capacities = sorted(
        (c for c in (_capacity_of(tid, t) for tid, t in data["teams"].items())
         if isinstance(c, int) and c > 0), reverse=True)
    if isinstance(capacity, int) and capacity > 0 and capacities:
        rank = 1 + sum(1 for c in capacities if c > capacity)
        stadium["capacity_pct"] = round(100 * capacity / capacities[0])
        stadium["capacity_rank"] = f"{rank}{ordinal_suffix(rank)} largest in the league"
        stadium["capacity_biggest"] = f"{capacities[0]:,}"

    # Manager and staff
    coach = profile.get("coach") or None
    if coach:
        contract = coach.get("contract") or {}
        coach = {**coach,
                 "age": _age(coach.get("date_of_birth")),
                 "initials": "".join(w[0] for w in (coach.get("name") or "?").split()[:2]).upper(),
                 "progress": _contract_progress(contract.get("start"), contract.get("until"))}

    # Honours: hand-kept list if present, otherwise the title count the API history gives
    honours = [
        {"name": h.get("name"), "count": h.get("count"), "kind": honour_kind(h.get("name"))}
        for h in extras.get("honours", [])
        if isinstance(h, dict) and h.get("name")
    ]
    trophies = {
        "honours": honours,  # hand-kept / generated only; no API has a trophy history
        "total": sum(h["count"] for h in honours if isinstance(h["count"], int)),
    }

    # Formation: a hand-set default wins; otherwise the club's most used formation (with the XI from its latest use).
    formation_data = entry.get("formation") or {}
    if extras.get("default_formation"):
        formation = build_formation(extras["default_formation"], extras.get("starting_xi"), squad)
        kind = "manual"
    else:
        formation = build_formation(formation_data.get("default"), formation_data.get("starting_xi"), squad)
        kind = "most_used"
    if formation:
        formation["kind"] = kind

    neighbours = []
    here = next((i for i, r in enumerate(table_rows) if r["team"]["id"] == team_id), None)
    if here is not None:
        start = max(0, min(here - 2, len(table_rows) - 5))
        neighbours = [{**r, "current": r["team"]["id"] == team_id} for r in table_rows[start:start + 5]]

    context = {
        "neighbours": neighbours,
        "team": team,
        "team_id": team_id,
        "table": table_rows,
        "stats": _season_stats(stats),
        "revenue": revenue,
        "stadium": stadium,
        "coach": coach,
        "assistants": assistant_coaches(extras),
        "trophies": trophies,
        "formation": formation,
        "squad_groups": group_squad(squad),
        "prediction": next_match_card(data, team_id),
        "total_players": len(squad),
        "default_layout": [
            {**widget, "url": reverse(PAGE_ROUTES[widget["id"]], args=[team_id])} if widget["id"] in PAGE_ROUTES else dict(widget)
            for widget in DEFAULT_LAYOUT
        ],
        "data_updated": (data.get("meta") or {}).get("generated_at"),
        "uses_footballdata_io": bool((entry.get("sources") or {}).get("footballdata_io")),
        "page_title": f"{profile.get('short_name') or profile.get('name', 'Team')} Dashboard",
    }
    return context, None


def _no_data_page(request, error):
    if error == "missing_data":
        messages.warning(request, "No Premier League data yet. Build it with: python manage.py build_pl_data")
    else:
        messages.error(request, error)
    return render(request, "dashboard.html", {
        "team": None, "data_missing": error == "missing_data",
        "page_title": "Dashboard",
    })


def dashboard_home(request):
    """Send the user to the last team they viewed, or the first club alphabetically."""
    data = load_pl_data()
    if data is None:
        return _no_data_page(request, "missing_data")

    session = getattr(request, "session", None)
    remembered = session.get("dashboard_team_id") if session is not None else None
    if remembered and str(remembered) in data["teams"]:
        return redirect("ufd_app:dashboard", team_id=remembered)

    first = sorted(data["teams"].values(), key=lambda t: t["profile"].get("name", ""))[0]
    return redirect("ufd_app:dashboard", team_id=first["profile"]["id"])


def dashboard_view(request, team_id):
    try:
        context, error = build_dashboard_context(team_id)
    except Exception:  # keep the page usable whatever the data file contains
        logger.exception("Dashboard failed for team %s", team_id)
        context, error = None, "Something went wrong while building the dashboard."

    if error:
        return _no_data_page(request, error)

    session = getattr(request, "session", None)
    if session is not None:
        session["dashboard_team_id"] = team_id
    return render(request, "dashboard.html", context)