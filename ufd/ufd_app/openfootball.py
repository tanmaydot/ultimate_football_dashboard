"""Historical Premier League fixtures and results from the open-source OpenFootball project.

Source: https://github.com/openfootball/england (CC0 / public domain), one plain-text file per season
(``2023-24/1-premierleague.txt``). Two layouts exist and both are read::

    20:00  Arsenal FC              v Coventry City FC         3-0 (2-0)     <- 2024-25, 2026-27
    20:00  Liverpool  4-2 (1-0)  Bournemouth                                <- 2000-01 ... 2023-24, 2025-26

A match without a score is an upcoming fixture. Team names are reduced to ``pl_data.normalise_team_name``
keys so that "Brighton & Hove Albion FC", "Brighton" and "Brighton & Hove Albion" are one club.

The parsed seasons are cached in ``data/openfootball.json`` (about 0.6 MB) so a build only downloads the
current season again.
"""
import logging
import re
from datetime import date

import requests

from .pl_data import normalise_team_name
from .util import DATA_DIR, read_json, utc_now_iso, write_json_atomic

logger = logging.getLogger(__name__)

REPO_URL = "https://github.com/openfootball/england"
RAW_URL = "https://raw.githubusercontent.com/openfootball/england/master/{season}/1-premierleague.txt"
FIRST_SEASON = 2000          # earlier seasons have 22 clubs and are not used
DEFAULT_ARCHIVE = DATA_DIR / "openfootball.json"
REQUEST_TIMEOUT = 30

_MONTHS = {m: i for i, m in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}
_DATE = re.compile(r"^(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\w*\s+([A-Z][a-z]{2})\s+(\d{1,2})(?:\s+(\d{4}))?\s*$")
_MATCHDAY = re.compile(r"^[▪•*]?\s*(?:Matchday|Regular Season\s*-)\s*(\d+)", re.I)
_TIME = re.compile(r"^\d{1,2}[:.]\d{2}\s+")
_SCORE = r"(?P<hg>\d+)-(?P<ag>\d+)(?:\s+\(\d+-\d+\))?"
_V_LINE = re.compile(rf"^(?P<h>.+?)\s+v\s+(?P<a>.+?)(?:\s+{_SCORE})?$")
_PLAIN_LINE = re.compile(rf"^(?P<h>.+?)\s+{_SCORE}\s+(?P<a>.+)$")


def season_label(start_year):
    return f"{start_year}-{(start_year + 1) % 100:02d}"


def season_start_year(label):
    return int(label[:4])


def parse_season(text, start_year):
    """Matches of one season as dicts: date (ISO), matchday, home, away (name keys), hg/ag (None = not played)."""
    matches, current, matchday = [], None, None
    for raw in text.replace("\r", "").split("\n"):
        line = raw.strip()
        if not line or line[0] in "=#(":
            continue
        found = _MATCHDAY.match(line)
        if found:
            matchday = int(found.group(1))
            continue
        found = _DATE.match(line)
        if found:
            month, day = _MONTHS[found.group(1)], int(found.group(2))
            year = int(found.group(3)) if found.group(3) else (start_year if month >= 8 else start_year + 1)
            current = date(year, month, day)
            continue
        body = _TIME.sub("", line)
        found = _V_LINE.match(body) if re.search(r"\s+v\s+", body) else _PLAIN_LINE.match(body)
        if not found or current is None:
            continue  # goal-scorer lines and anything else that is not a match
        home, away = normalise_team_name(found.group("h")), normalise_team_name(found.group("a"))
        if not home or not away:
            continue
        hg, ag = found.group("hg"), found.group("ag")
        matches.append({"date": current.isoformat(), "matchday": matchday, "home": home, "away": away,
                        "hg": int(hg) if hg is not None else None, "ag": int(ag) if ag is not None else None})
    return matches


def fetch_season_text(start_year, session=None):
    url = RAW_URL.format(season=season_label(start_year))
    response = (session or requests).get(url, timeout=REQUEST_TIMEOUT)
    if response.status_code != 200:
        raise OSError(f"{url}: HTTP {response.status_code}")
    return response.content.decode("utf-8")


def current_season_start(today=None):
    today = today or date.today()
    return today.year if today.month >= 8 else today.year - 1


def sanity_problems(matches):
    """What is wrong with one parsed season (an empty list when it is a normal 20-club season)."""
    teams = {m["home"] for m in matches} | {m["away"] for m in matches}
    problems = []
    if len(teams) != 20:
        problems.append(f"{len(teams)} clubs instead of 20")
    if len(matches) != 380:
        problems.append(f"{len(matches)} matches instead of 380")
    if len({(m["home"], m["away"]) for m in matches}) != len(matches):
        problems.append("a home/away pairing appears twice")
    return problems


class Archive:
    """The cached seasons: ``{label: [match, ...]}`` plus where and when they were fetched."""

    def __init__(self, seasons=None, meta=None):
        self.seasons = seasons or {}
        self.meta = meta or {}

    @classmethod
    def load(cls, path):
        try:
            body = read_json(path)
        except (OSError, ValueError):
            return cls()
        seasons = {label: [dict(zip(body["columns"], row)) for row in rows] for label, rows in (body.get("seasons") or {}).items()}
        return cls(seasons, body.get("meta"))

    def save(self, path):
        columns = ["date", "matchday", "home", "away", "hg", "ag"]
        write_json_atomic(path, {
            "meta": {**self.meta, "source": REPO_URL, "licence": "CC0 1.0 (public domain)", "saved_at": utc_now_iso()},
            "columns": columns,
            "seasons": {label: [[m[c] for c in columns] for m in rows] for label, rows in sorted(self.seasons.items())},
        }, indent=None)

    def update(self, today=None, fetch=fetch_season_text, force=False, log=logger.info):
        """Download what is missing (every finished season once) and the current season every time.

        Returns the list of labels that were fetched. A season that fails to download or parse keeps
        its earlier copy; a season that does not look like a Premier League season is rejected.
        """
        current = current_season_start(today)
        fetched = []
        for year in range(FIRST_SEASON, current + 1):
            label = season_label(year)
            if label in self.seasons and year < current and not force:
                continue
            try:
                matches = parse_season(fetch(year), year)
            except (OSError, requests.RequestException, UnicodeDecodeError) as exc:
                log(f"openfootball {label}: not fetched ({exc})")
                continue
            problems = sanity_problems(matches)
            if problems and year < current:
                log(f"openfootball {label}: rejected ({'; '.join(problems)})")
                continue
            self.seasons[label] = matches
            fetched.append(label)
        self.meta["current_season"] = season_label(current)
        return fetched

    def played(self):
        """Every finished match of every cached season, oldest first."""
        rows = [m for matches in self.seasons.values() for m in matches if m["hg"] is not None]
        return sorted(rows, key=lambda m: (m["date"], m["home"]))

    def fixtures(self, label):
        return [m for m in self.seasons.get(label, []) if m["hg"] is None]
