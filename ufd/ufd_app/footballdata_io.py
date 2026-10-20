"""Client for footballdata.io (API v1).

Auth is a Bearer token in the Authorization header. The key comes from the
FOOTBALLDATA_IO_API_KEY setting / environment variable and is never logged,
written to disk, or included in an error message.

Every response is an envelope: {"success": true, "data": ..., "meta": {...}} and
list endpoints add "pagination": {"page", "limit", "total", "total_pages"}.
Errors are {"success": false, "error": {"code": ..., "message": ...}}.

The documentation doesn't publish the field names inside ``data``; the ones used by
``FootballDataIoSource`` below come from real responses captured with
``python manage.py probe_footballdata_io`` (October 2026).
"""
import html
import logging
import os
import time

import requests
from django.conf import settings

from .util import Throttle

logger = logging.getLogger(__name__)

BASE_URL = "https://footballdata.io/api/v1"
MIN_INTERVAL = 0.5  # the per-minute limit isn't documented, so stay gentle
REQUEST_TIMEOUT = 20

_STATUS_MESSAGES = {
    401: "the API key doesn't exist",
    403: "the API key is inactive",
    429: "the monthly request limit has been reached",
}


class FootballDataIoError(Exception):
    """A request failed. ``status`` is the HTTP status when there was one. Never contains the key."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class FootballDataIoClient:
    def __init__(self, api_key=None, min_interval=MIN_INTERVAL, sleep=time.sleep, clock=time.monotonic):
        self.api_key = (api_key or getattr(settings, "FOOTBALLDATA_IO_API_KEY", "")
                        or os.environ.get("FOOTBALLDATA_IO_API_KEY", ""))
        self.throttle = Throttle(min_interval, sleep, clock)

    def is_configured(self):
        return bool(self.api_key)

    def _scrub(self, text):
        text = str(text or "")[:200]
        return text.replace(self.api_key, "[key]") if self.api_key else text

    def get(self, path, params=None):
        """GET one endpoint and return the full response envelope (a dict)."""
        if not self.api_key:
            raise FootballDataIoError("FOOTBALLDATA_IO_API_KEY is not set")
        self.throttle.wait()
        try:
            response = requests.get(
                BASE_URL + path,
                headers={"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"},
                params=params,
                timeout=REQUEST_TIMEOUT,
            )
        except requests.exceptions.RequestException as exc:
            # str(exc) can include the request URL, so only the type is reported.
            raise FootballDataIoError(f"footballdata.io {path}: request failed ({type(exc).__name__})")
        finally:
            self.throttle.mark()

        status = response.status_code
        try:
            body = response.json()
        except ValueError:
            body = None

        if status != 200 or not isinstance(body, dict) or body.get("success") is False:
            error = body.get("error") if isinstance(body, dict) and isinstance(body.get("error"), dict) else {}
            reason = _STATUS_MESSAGES.get(status) or self._scrub(error.get("message")) or f"HTTP {status}"
            code = self._scrub(error.get("code"))
            raise FootballDataIoError(
                f"footballdata.io {path}: {reason}" + (f" [{code}]" if code else ""), status=status
            )
        return body

    def get_all(self, path, params=None, limit=100, max_pages=20):
        """Follow pagination and return every item of a list endpoint.

        Stops at ``max_pages`` so a surprise 10,000-row endpoint can't burn the monthly allowance.
        """
        items, page = [], 1
        while page <= max_pages:
            body = self.get(path, {**(params or {}), "page": page, "limit": limit})
            data = body.get("data")
            if not isinstance(data, list):
                return data if page == 1 else items
            items.extend(data)
            # The real API puts pagination under "meta"; the docs show it at the top level.
            paging = body.get("pagination") or (body.get("meta") or {}).get("pagination") or {}
            total_pages = paging.get("total_pages") or 1
            if page >= total_pages:
                break
            page += 1
        return items


def clean_name(value):
    """Names arrive HTML-escaped ("Brighton &amp; Hove Albion")."""
    return html.unescape(value or "").strip()


class FootballDataIoSource:
    """The few footballdata.io calls the Premier League build needs (about 23 requests in all).

    Every method returns plain data, or None when the API's answer isn't what was asked for.
    Findings from the real responses that this class works around:
      * /leagues/{id}/teams lists a team once per season, so rows are filtered to the latest season;
      * /teams/{id}/stats defaults to the team's most recent competition (the Champions League for
        top clubs), so league_id and season_id are always passed and the answer is checked;
      * stadium names are empty, and standings' points_per_game is 0, so neither is used.
    """

    def __init__(self, client=None):
        self.client = client or FootballDataIoClient()

    def is_configured(self):
        return self.client.is_configured()

    def premier_league_id(self):
        body = self.client.get("/countries/england/leagues")
        for league in (body.get("data") or {}).get("leagues") or []:
            if isinstance(league, dict) and league.get("competition_name") == "Premier League":
                return league.get("league_id")
        return None

    def get_current_teams(self, league_id):
        """(season_id, [team, ...]) for the latest season, one entry per club."""
        body = self.client.get(f"/leagues/{league_id}/teams", {"limit": 100})
        rows = [t for t in (body.get("data") or {}).get("teams") or [] if isinstance(t, dict) and t.get("team_id")]
        years = [(t.get("season") or {}).get("year") for t in rows if isinstance((t.get("season") or {}).get("year"), int)]
        if not years:
            return None, []
        latest = max(years)
        season_id = next((t["season"].get("season_id") for t in rows if t["season"].get("year") == latest), None)
        seen, teams = set(), []
        for team in rows:
            if (team.get("season") or {}).get("season_id") == season_id and team["team_id"] not in seen:
                seen.add(team["team_id"])
                teams.append(team)
        return season_id, teams

    def get_team_stats(self, team_id, league_id, season_id):
        """The team's stats for exactly this league and season, or None if the API answered with another."""
        body = self.client.get(f"/teams/{team_id}/stats", {"league_id": league_id, "season_id": season_id})
        data = body.get("data")
        if not isinstance(data, dict):
            return None
        if (data.get("league") or {}).get("league_id") != league_id or (data.get("season") or {}).get("season_id") != season_id:
            return None
        return data
