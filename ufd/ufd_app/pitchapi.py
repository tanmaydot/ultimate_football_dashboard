"""Client for PitchAPI (https://pitchapi.dev), a read-only football REST API.

Auth is the ``X-API-KEY`` header; the key comes from the PITCHAPI_API_KEY setting / environment
variable and is never logged, written to disk, or included in an error message.

Every success is ``{"data": ...}`` and every failure ``{"error": {"code", "message"}}``. The API has no
quota, only an unadvertised fair-use burst guard: it answers 429 with a ``Retry-After`` header, which
this client honours (and slows itself down afterwards).
"""
import logging
import os
import random
import time

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

BASE_URL = "https://api.pitchapi.dev"
MIN_INTERVAL = 0.3      # seconds between calls
MAX_INTERVAL = 5.0      # the self-imposed pause never grows beyond this
REQUEST_TIMEOUT = 30
MAX_RETRY_AFTER = 120   # never sleep longer than this for one 429


class PitchApiError(Exception):
    """A request failed. ``status`` is the HTTP status, ``code`` the API's error code. Never contains the key."""

    def __init__(self, message, status=None, code=None):
        super().__init__(message)
        self.status = status
        self.code = code


class PitchApiAuthError(PitchApiError):
    """401: nothing else will work with this key, so callers should stop."""


def get_api_key():
    return getattr(settings, "PITCHAPI_API_KEY", "") or os.environ.get("PITCHAPI_API_KEY", "")


class PitchApiClient:
    def __init__(self, api_key=None, base_url=None, session=None, min_interval=MIN_INTERVAL, max_retries=5,
                 sleep=time.sleep, clock=time.monotonic, timeout=REQUEST_TIMEOUT):
        self._key = api_key if api_key is not None else get_api_key()
        self.base_url = (base_url or os.environ.get("PITCHAPI_BASE_URL") or BASE_URL).rstrip("/")
        self.session = session or requests.Session()
        self.min_interval = min_interval
        self.interval = min_interval
        self.max_retries = max_retries
        self.sleep = sleep
        self.clock = clock
        self.timeout = timeout
        self.requests_made = 0
        self.rate_limited = 0
        self._last = None

    def is_configured(self):
        return bool(self._key)

    def _wait_turn(self):
        if self._last is not None:
            remaining = self.interval - (self.clock() - self._last)
            if remaining > 0:
                self.sleep(remaining)

    def get(self, path, params=None):
        """GET one path and return the decoded envelope. Raises PitchApiError (never leaks the key)."""
        if not self._key:
            raise PitchApiAuthError("PITCHAPI_API_KEY is not set", status=401, code="UNAUTHORIZED")
        url = f"{self.base_url}{path}"
        last_problem = "no attempt made"
        for attempt in range(self.max_retries + 1):
            self._wait_turn()
            try:
                response = self.session.get(url, params=params, headers={"X-API-KEY": self._key}, timeout=self.timeout)
            except requests.exceptions.RequestException as exc:
                # str(exc) can contain the URL; the key is only ever in a header, but log the type only to be safe.
                last_problem = f"network error ({type(exc).__name__})"
                self._last = self.clock()
                self.sleep(min(2 ** attempt, 30) + random.random())
                continue
            self.requests_made += 1
            self._last = self.clock()
            status = response.status_code

            if status == 429:
                self.rate_limited += 1
                try:
                    wait = float(response.headers.get("Retry-After") or 5)
                except (TypeError, ValueError, AttributeError):
                    wait = 5.0
                wait = max(1.0, min(wait, MAX_RETRY_AFTER))
                self.interval = min(MAX_INTERVAL, self.interval * 2)  # be gentler for the rest of the run
                last_problem = f"HTTP 429 (waited {wait:.0f}s)"
                logger.warning("pitchapi %s rate limited; waiting %.0fs", path, wait)
                self.sleep(wait)
                continue
            if status >= 500:
                last_problem = f"HTTP {status}"
                self.sleep(min(2 ** attempt, 30) + random.random())
                continue

            try:
                body = response.json()
            except ValueError:
                raise PitchApiError(f"{path}: HTTP {status} with a body that is not JSON", status=status)
            error = body.get("error") if isinstance(body, dict) else None
            if status == 401:
                raise PitchApiAuthError(f"{path}: the API key was rejected (HTTP 401)", status=401, code="UNAUTHORIZED")
            if status >= 400 or error:
                error = error if isinstance(error, dict) else {}
                code = error.get("code")
                raise PitchApiError(f"{path}: HTTP {status} {code or ''} {error.get('message') or ''}".strip(),
                                    status=status, code=code)
            if not isinstance(body, dict) or "data" not in body:
                raise PitchApiError(f"{path}: unexpected response shape", status=status)
            # a calm stretch: let the pause shrink back towards the minimum
            self.interval = max(self.min_interval, self.interval * 0.9)
            return body
        raise PitchApiError(f"{path}: gave up after {self.max_retries + 1} attempts ({last_problem})", status=None)
