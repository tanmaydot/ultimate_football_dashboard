"""Refresh the generated half of the club extras (``data/club_extras.auto.json``).

Run it with ``python manage.py refresh_club_extras``.

What it fills in, and from where
    stadium   Transfermarkt API (capacity, opened, city); Wikidata is a fallback and a
              cross-check that flags suspicious capacities. These almost never change.
    honours   Transfermarkt API ``/clubs/{id}/achievements``. This is the part
              that really changes (whenever a trophy is won).

What it never touches: revenue, assistant coaches, default_formation, starting_xi.
No free source has revenue or staff, and the formation is derived from real
lineups by ``build_pl_data``. Keep those in the hand-kept ``club_extras.json``,
which always wins over this file (see ``dashboard.load_club_extras``).

The Transfermarkt API (transfermarkt-api.fly.dev) is an unofficial scraper of
Transfermarkt, hosted for free, so this is meant to run once a quarter (1 Jan, Apr, Jul, Oct), not on every
request. If a club's fetch fails, its previous entry is kept, so an outage can
never wipe data.
"""
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from .util import read_json, write_json_atomic

logger = logging.getLogger(__name__)

AUTO_PATH = Path(__file__).resolve().parent / "data" / "club_extras.auto.json"
TM_API = "https://transfermarkt-api.fly.dev"
WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
UA = "ultimate-football-dashboard/1.0 (club extras refresh; python-requests)"

# football-data.org team id -> Transfermarkt club id / Wikidata item.
# Taken from a probe run and checked by eye (every name matched). A club that is
# promoted into the league later and is missing here is simply skipped and listed
# in the run summary; add its two ids and it is picked up on the next run.
CLUB_IDS = {
    "57":   {"tm": "11",   "wd": "Q9617"},     # Arsenal
    "58":   {"tm": "405",  "wd": "Q18711"},    # Aston Villa
    "61":   {"tm": "631",  "wd": "Q9616"},     # Chelsea
    "62":   {"tm": "29",   "wd": "Q5794"},     # Everton
    "63":   {"tm": "931",  "wd": "Q18708"},    # Fulham
    "64":   {"tm": "31",   "wd": "Q1130849"},  # Liverpool
    "65":   {"tm": "281",  "wd": "Q50602"},    # Manchester City
    "66":   {"tm": "985",  "wd": "Q18656"},    # Manchester United
    "67":   {"tm": "762",  "wd": "Q18716"},    # Newcastle United
    "71":   {"tm": "289",  "wd": "Q18739"},    # Sunderland
    "73":   {"tm": "148",  "wd": "Q18741"},    # Tottenham Hotspur
    "322":  {"tm": "3008", "wd": "Q19477"},    # Hull City
    "341":  {"tm": "399",  "wd": "Q1128631"},  # Leeds United
    "349":  {"tm": "677",  "wd": "Q9653"},     # Ipswich Town
    "351":  {"tm": "703",  "wd": "Q19490"},    # Nottingham Forest
    "354":  {"tm": "873",  "wd": "Q19467"},    # Crystal Palace
    "397":  {"tm": "1237", "wd": "Q19453"},    # Brighton (name search fails on the "&")
    "402":  {"tm": "1148", "wd": "Q19571"},    # Brentford
    "1044": {"tm": "989",  "wd": "Q19568"},    # AFC Bournemouth
    "1076": {"tm": "990",  "wd": "Q19580"},    # Coventry City
}

# Transfermarkt achievement title -> the name shown on the dashboard.
# Several titles can share a name; their counts are added together (the old
# European Cup and the Champions League are one competition). None = skip.
# Titles that are not listed here are skipped but reported in the run summary.
TM_TITLE_MAP = {
    "english champion": "English League Champions",   # all top-flight titles, pre- and post-1992
    "uefa champions league winner": "European Cup / Champions League",
    "european champion clubs' cup winner": "European Cup / Champions League",
    "uefa cup winner": "UEFA Cup",
    "europa league winner": "Europa League",
    "cup winners cup winner": "Cup Winners' Cup",
    "conference league winner": "Conference League",
    "inter-cities fairs cup winner": "Inter-Cities Fairs Cup",
    "uefa supercup winner": "UEFA Super Cup",
    "fifa club world cup winner": "Club World Cup",
    "intercontinental cup winner": "Intercontinental Cup",
    "fa cup winner": "FA Cup",
    "english league cup winner": "League Cup",
    "english supercup winner": "Community Shield",
    "english 2nd tier champion": "Second Tier Champions",
    # Deliberately left out of the widget (change to a name to show them):
    "english 3rd tier champion": None,
    "english 4th tier champion": None,
    "football league trophy winner": None,
    "intertoto cup champion": None,
}
# Display order. Names not listed here go last, alphabetically.
HONOUR_ORDER = [
    "English League Champions", "FA Cup", "League Cup", "Community Shield",
    "European Cup / Champions League", "UEFA Cup", "Europa League", "Cup Winners' Cup",
    "Conference League", "Inter-Cities Fairs Cup", "UEFA Super Cup", "Club World Cup",
    "Intercontinental Cup", "Second Tier Champions",
]

# If the two sources disagree on a capacity by more than this, the club is flagged.
CAPACITY_DISAGREE = 0.05

_POSTCODE_PREFIX = re.compile(r"^[A-Z]{1,2}\d[A-Z\d]?\s*[0-9O][A-Z0-9]{2}\s+", re.I)


class Fetcher:
    """Small polite HTTP helper (retries, spacing between calls)."""

    def __init__(self, session=None, pause=1.0):
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": UA, "Accept": "application/json"})
        self.pause = pause

    def get_json(self, url, params=None, retries=3):
        last = None
        for attempt in range(retries):
            try:
                resp = self.session.get(url, params=params, timeout=45)
                if resp.status_code == 429 or resp.status_code >= 500:
                    last = f"HTTP {resp.status_code}"
                    time.sleep(2.0 * (attempt + 1))
                    continue
                resp.raise_for_status()
                time.sleep(self.pause)
                return resp.json()
            except (requests.RequestException, ValueError) as exc:
                last = str(exc)
                time.sleep(2.0 * (attempt + 1))
        raise RuntimeError(f"{url}: {last}")


# --------------------------------------------------------------------------- #
# Parsing helpers (pure functions, unit-tested)
# --------------------------------------------------------------------------- #
def clean_city(address_line):
    """'N5 1BU London' -> 'London'. Transfermarkt puts the UK postcode first."""
    text = re.sub(r"\s+", " ", (address_line or "")).strip()
    text = _POSTCODE_PREFIX.sub("", text).strip()
    return text or None


def _positive_int(value):
    if value in (None, ""):
        return None
    try:
        number = int(float(str(value).replace(",", "")))
    except ValueError:
        return None
    return number if number > 0 else None


def _norm_venue(name):
    name = (name or "").lower().replace("’", "'")
    return re.sub(r"[^a-z0-9]+", "", re.sub(r"\b(the|stadium|ground|park)\b", "", name))


def parse_tm_stadium(profile):
    st = profile.get("stadium") or {}
    return {
        "venue": st.get("name") or profile.get("stadiumName"),
        "capacity": _positive_int(st.get("capacity")),
        "opened": _positive_int(st.get("buildYear")),
        "city": clean_city(st.get("addressLine2")),
    }


def choose_stadium(tm, wd):
    """Merge the two sources. Returns (stadium_dict, review_note_or_None).

    Transfermarkt is the single source of truth whenever it has a value: its opened
    year and city are right where Wikidata's are wrong (Fulham 1780, Everton 2021,
    'Rushcliffe'), and Wikidata still listed Brentford's old ground. Wikidata is only
    a fallback and a cross-check: when both have a capacity for the same ground and
    differ by more than 5% (the probe found Aston Villa, Man City and Fulham), the
    note asks you to confirm the figure and, if needed, set it in club_extras.json.
    """
    tm, wd = tm or {}, wd or {}
    out, note = {}, None
    # The ground's name too: the football-data name goes stale after a move (it still says
    # Goodison Park / Griffin Park / Kingston Communications Stadium), which would sit next
    # to the new ground's capacity. Wikidata's name is never used - it can be stale as well.
    if tm.get("venue"):
        out["name"] = tm["venue"]
    tm_cap, wd_cap = tm.get("capacity"), wd.get("capacity")
    same_ground = bool(tm.get("venue") and wd.get("venue") and _norm_venue(tm["venue"]) == _norm_venue(wd["venue"]))
    capacity = tm_cap or wd_cap
    if capacity:
        out["capacity"] = capacity
    if tm_cap and wd_cap and same_ground and abs(tm_cap - wd_cap) / max(tm_cap, wd_cap) > CAPACITY_DISAGREE:
        note = f"capacity differs: Transfermarkt {tm_cap:,} vs Wikidata {wd_cap:,} - check the club's own site"
    city = tm.get("city")
    if city:
        out["city"] = city
    opened = tm.get("opened") or (wd.get("opened") if (not tm or same_ground) else None)
    if opened:
        out["opened"] = opened
    return out, note


def map_honours(achievements):
    """Transfermarkt achievements -> ([{name, count}], [unmapped titles])."""
    totals, unmapped = {}, []
    for item in achievements or []:
        title = (item.get("title") or "").strip()
        if not title:
            continue  # the API returns many untitled groups; nothing to name them by
        key = title.lower()
        if key not in TM_TITLE_MAP:
            unmapped.append(title)
            continue
        name = TM_TITLE_MAP[key]
        count = item.get("count")
        if name and isinstance(count, int) and count > 0:
            totals[name] = totals.get(name, 0) + count
    rank = {n: i for i, n in enumerate(HONOUR_ORDER)}
    ordered = sorted(totals.items(), key=lambda kv: (rank.get(kv[0], len(rank)), kv[0]))
    return [{"name": n, "count": c} for n, c in ordered], unmapped


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #
def fetch_wikidata_stadium(fetch, qid):
    query = f"""
        SELECT ?venueLabel ?cap ?inc WHERE {{
          wd:{qid} p:P115 ?vs . ?vs ps:P115 ?venue .
          FILTER NOT EXISTS {{ ?vs pq:P582 ?ended }}
          OPTIONAL {{ ?venue wdt:P1083 ?cap }}
          OPTIONAL {{ ?venue wdt:P571 ?inc }}
          SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
        }}"""
    rows = fetch.get_json(WIKIDATA_SPARQL, params={"query": query, "format": "json"})["results"]["bindings"]
    if not rows:
        return None
    best = max(rows, key=lambda r: _positive_int(r.get("cap", {}).get("value")) or -1)
    inc = best.get("inc", {}).get("value", "")
    return {
        "venue": best.get("venueLabel", {}).get("value"),
        "capacity": _positive_int(best.get("cap", {}).get("value")),
        "opened": int(inc[:4]) if re.match(r"^\d{4}", inc) else None,
        "city": None,  # Wikidata's value is a council area; never used
    }


def refresh_club(fetch, ids):
    """Fetch one club. Returns (entry, notes, unmapped); raises if nothing could be fetched."""
    notes, tm_stadium, wd_stadium, honours, unmapped = [], None, None, None, []
    errors = []
    try:
        profile = fetch.get_json(f"{TM_API}/clubs/{ids['tm']}/profile")
        tm_stadium = parse_tm_stadium(profile)
    except Exception as exc:  # noqa: BLE001 - keep going with the other source
        errors.append(f"transfermarkt profile: {exc}")
    try:
        ach = fetch.get_json(f"{TM_API}/clubs/{ids['tm']}/achievements").get("achievements") or []
        honours, unmapped = map_honours(ach)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"transfermarkt achievements: {exc}")
    try:
        wd_stadium = fetch_wikidata_stadium(fetch, ids["wd"])
    except Exception as exc:  # noqa: BLE001
        errors.append(f"wikidata: {exc}")

    entry = {}
    stadium, note = choose_stadium(tm_stadium, wd_stadium)
    if stadium:
        entry["stadium"] = stadium
    if note:
        notes.append(note)
    if honours:
        entry["honours"] = honours
    if errors and not entry:
        raise RuntimeError("; ".join(errors))
    if errors:
        notes.append("partial: " + "; ".join(errors))
    return entry, notes, unmapped


# --------------------------------------------------------------------------- #
# File handling
# --------------------------------------------------------------------------- #
def read_auto(path=AUTO_PATH):
    try:
        data = read_json(path)
    except (FileNotFoundError, ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def write_auto(data, path=AUTO_PATH):
    write_json_atomic(path, data)


def refresh_all(fetch, team_ids, existing, now=None):
    """Return (new_file_dict, summary). A failed club keeps its previous entry."""
    now = now or datetime.now(timezone.utc)
    result = {k: v for k, v in existing.items() if k.startswith("_")}
    summary = {"ok": [], "failed": {}, "skipped_no_ids": [], "review": {}, "unmapped": {}}
    for tid in team_ids:
        ids = CLUB_IDS.get(str(tid))
        if not ids:
            summary["skipped_no_ids"].append(str(tid))
            if str(tid) in existing:
                result[str(tid)] = existing[str(tid)]
            continue
        try:
            entry, notes, unmapped = refresh_club(fetch, ids)
        except Exception as exc:  # noqa: BLE001
            summary["failed"][str(tid)] = str(exc)
            if str(tid) in existing:
                result[str(tid)] = existing[str(tid)]  # keep the last good data
            continue
        entry["_updated"] = now.strftime("%Y-%m-%d")
        if notes:
            entry["_review"] = notes
            summary["review"][str(tid)] = notes
        result[str(tid)] = entry
        summary["ok"].append(str(tid))
        if unmapped:
            summary["unmapped"][str(tid)] = unmapped
    result["_readme"] = ("GENERATED by `python manage.py refresh_club_extras` - do not edit by hand. "
                         "Anything in club_extras.json overrides this file. Keys starting with _ are ignored.")
    result["_generated_at"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    return result, summary