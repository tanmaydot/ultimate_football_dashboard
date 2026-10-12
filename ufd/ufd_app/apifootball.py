"""Client for apifootball.com (APIfootball v3).

The API key is read from the APIFOOTBALL_API_KEY setting / environment variable
and is never written to the logs: apifootball takes the key as a URL query
parameter, so request errors (whose text includes the URL) are logged by type
only.
"""
import logging
import os

import requests
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

API_URL = "https://apiv3.apifootball.com/"
ENGLAND_COUNTRY_ID = 44
REQUEST_TIMEOUT = 15  # seconds
LEAGUES_CACHE_SECONDS = 24 * 60 * 60

# Temporary bridge: the teams / standings / matches pages still run on
# football-data.org, which identifies leagues by a short code. Leagues without
# an entry here are listed but show "coming soon" until those pages move over.
FOOTBALL_DATA_CODES = {
    "Premier League": "PL",
    "Championship": "ELC",
}


def get_api_key():
    return getattr(settings, "APIFOOTBALL_API_KEY", "") or os.environ.get("APIFOOTBALL_API_KEY", "")


def is_configured():
    return bool(get_api_key())


def make_api_request(action, **params):
    """Call one apifootball action. Returns the decoded JSON list, or None on any failure."""
    key = get_api_key()
    if not key:
        logger.error("APIFOOTBALL_API_KEY is not set")
        return None

    try:
        response = requests.get(
            API_URL,
            params={"action": action, **params, "APIkey": key},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.exceptions.RequestException as exc:
        # Do not log str(exc): it contains the request URL, and therefore the key.
        logger.error("apifootball request '%s' failed: %s", action, type(exc).__name__)
        return None

    if response.status_code != 200:
        logger.error("apifootball request '%s' returned HTTP %s", action, response.status_code)
        return None

    try:
        data = response.json()
    except ValueError:
        logger.error("apifootball request '%s' returned invalid JSON", action)
        return None

    # Success is a JSON list. Errors (bad key, no data, plan limits) come back as an object
    # such as {"error": 404, "message": "..."}.
    if not isinstance(data, list):
        message = data.get("message") if isinstance(data, dict) else None
        logger.error("apifootball request '%s' was rejected: %s", action, message or "unexpected response")
        return None
    return data


def _normalise_league(raw):
    """Map an apifootball league onto the shape the leagues templates already use."""
    name = raw.get("league_name") or "Unknown league"
    return {
        "apifootball_id": raw.get("league_id"),
        "name": name,
        "emblem": raw.get("league_logo") or "",
        "season": raw.get("league_season") or "",
        "area": {
            "id": raw.get("country_id"),
            "name": raw.get("country_name") or "",
            "flag": raw.get("country_logo") or "",
        },
        # football-data.org code (or None) so the existing pages keep working for now.
        "code": FOOTBALL_DATA_CODES.get(name),
    }


def get_england_leagues():
    """England's competitions as listed by apifootball (country_id=44), cached for a day."""
    cache_key = f"apifootball_leagues_{ENGLAND_COUNTRY_ID}"
    try:
        cached = cache.get(cache_key)
    except Exception as exc:  # a broken cache must never break the page
        logger.warning("Cache read failed: %s", type(exc).__name__)
        cached = None
    if cached:
        return cached

    data = make_api_request("get_leagues", country_id=ENGLAND_COUNTRY_ID)
    if not data:
        return []

    leagues = [_normalise_league(item) for item in data if isinstance(item, dict)]
    try:
        cache.set(cache_key, leagues, LEAGUES_CACHE_SECONDS)
    except Exception as exc:
        logger.warning("Cache write failed: %s", type(exc).__name__)
    return leagues
