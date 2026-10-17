import json
import tempfile
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import StringIO
from pathlib import Path
from unittest import mock

import requests
from django.core.management import CommandError, call_command
from django.test import SimpleTestCase

from . import pitchapi, pitchapi_harvest

KEY = "pk_test_sekrit-key-123"


def reply(status=200, body=None, headers=None):
    r = mock.Mock()
    r.status_code = status
    r.headers = headers or {}
    r.json.return_value = body if body is not None else {"data": {}}
    return r


def client_with(*replies, **kw):
    session = mock.Mock()
    session.get.side_effect = list(replies)
    slept = []
    client = pitchapi.PitchApiClient(api_key=KEY, session=session, sleep=slept.append, min_interval=0, **kw)
    return client, session, slept


class ClientTests(SimpleTestCase):
    def test_sends_key_in_header_and_returns_the_envelope(self):
        client, session, _ = client_with(reply(body={"data": {"x": 1}}))
        self.assertEqual(client.get("/v1/leagues"), {"data": {"x": 1}})
        args, kwargs = session.get.call_args
        self.assertEqual(args[0], "https://api.pitchapi.dev/v1/leagues")
        self.assertEqual(kwargs["headers"], {"X-API-KEY": KEY})
        self.assertNotIn(KEY, args[0])  # never in the URL

    def test_429_waits_for_retry_after_then_succeeds_and_slows_down(self):
        client, _, slept = client_with(reply(429, {"error": {"code": "RATE_LIMIT_EXCEEDED"}}, {"Retry-After": "7"}), reply())
        client.get("/v1/leagues")
        self.assertIn(7.0, slept)
        self.assertEqual(client.rate_limited, 1)

    def test_5xx_is_retried_and_gives_up_cleanly(self):
        client, session, _ = client_with(*[reply(500, {"error": {"code": "INTERNAL_SERVER_ERROR"}})] * 3, max_retries=2)
        with self.assertRaises(pitchapi.PitchApiError) as ctx:
            client.get("/v1/leagues")
        self.assertEqual(session.get.call_count, 3)
        self.assertNotIn(KEY, str(ctx.exception))

    def test_401_is_an_auth_error_and_not_retried(self):
        client, session, _ = client_with(reply(401, {"error": {"code": "UNAUTHORIZED", "message": "bad"}}))
        with self.assertRaises(pitchapi.PitchApiAuthError):
            client.get("/v1/leagues")
        self.assertEqual(session.get.call_count, 1)

    def test_404_carries_the_api_error_code(self):
        client, _, _ = client_with(reply(404, {"error": {"code": "ANALYTICS_UNAVAILABLE", "message": "never rated"}}))
        with self.assertRaises(pitchapi.PitchApiError) as ctx:
            client.get("/v1/matches/m_1/advanced")
        self.assertEqual((ctx.exception.status, ctx.exception.code), (404, "ANALYTICS_UNAVAILABLE"))

    def test_network_errors_never_leak_the_key(self):
        session = mock.Mock()
        session.get.side_effect = requests.exceptions.ConnectionError(f"boom {KEY}")
        client = pitchapi.PitchApiClient(api_key=KEY, session=session, sleep=lambda s: None, min_interval=0, max_retries=1)
        with self.assertRaises(pitchapi.PitchApiError) as ctx:
            client.get("/v1/leagues")
        self.assertNotIn(KEY, str(ctx.exception))

    def test_missing_key(self):
        client = pitchapi.PitchApiClient(api_key="", session=mock.Mock())
        self.assertFalse(client.is_configured())
        with self.assertRaises(pitchapi.PitchApiAuthError):
            client.get("/v1/leagues")


class HelperTests(SimpleTestCase):
    def test_find_league_prefers_the_english_one(self):
        body = {"data": {"leagues": [
            {"id": "l_aaa", "name": "Premier League", "country": "Egypt"},
            {"id": "l_bbb", "name": "Premier League", "country": "England"}]}}
        self.assertEqual(pitchapi_harvest.find_league(body)["id"], "l_bbb")
        self.assertIsNone(pitchapi_harvest.find_league({"data": []}))

    def test_extract_seasons_from_any_shape_newest_first(self):
        league = {"id": "l_1", "seasons": ["2023/2024", {"name": "2025/2026"}, {"label": "2024/2025"}], "note": "x"}
        self.assertEqual(pitchapi_harvest.extract_seasons(league), ["2025/2026", "2024/2025", "2023/2024"])

    def test_match_ids_are_unique_and_ordered(self):
        body = {"data": {"matches": [{"id": "m_1"}, {"id": "m_2", "home_team": {"id": "t_1"}}, {"id": "m_1"}]}}
        self.assertEqual(pitchapi_harvest.match_ids(body), ["m_1", "m_2"])


# --------------------------------------------------------------------------- #
# End to end against a small fake PitchAPI on localhost
# --------------------------------------------------------------------------- #
LEAGUES = {"data": {"leagues": [{"id": "l_PL", "name": "Premier League", "country": "England",
                                 "seasons": ["2026/2027", "2025/2026"]}]}}
MATCHES = {"data": {"league": {"id": "l_PL"}, "matches": [
    {"id": "m_1", "home_team": {"id": "t_A", "name": "A"}, "away_team": {"id": "t_B", "name": "B"}},
    {"id": "m_2", "home_team": {"id": "t_B", "name": "B"}, "away_team": {"id": "t_A", "name": "A"}}]}}
LINEUPS = {"data": {"home": {"formation": "4-3-3", "starters": [{"player_id": "p_9", "name": "X"}]}, "away": {"starters": []}}}


class FakeApi(BaseHTTPRequestHandler):
    calls = []
    hit_429 = False

    def log_message(self, *a):
        pass

    def send(self, status, body, headers=None):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        type(self).calls.append(self.path)
        if self.headers.get("X-API-KEY") != KEY:
            return self.send(401, {"error": {"code": "UNAUTHORIZED", "message": "no"}})
        path = self.path.split("?")[0]
        if path == "/v1/leagues":
            if not type(self).hit_429:
                type(self).hit_429 = True
                return self.send(429, {"error": {"code": "RATE_LIMIT_EXCEEDED"}}, {"Retry-After": "0"})
            return self.send(200, LEAGUES)
        if path == "/v1/leagues/l_PL":
            return self.send(200, {"data": LEAGUES["data"]["leagues"][0]})
        if path == "/v1/leagues/l_PL/matches":
            return self.send(200, MATCHES if "status=played" in self.path else {"data": {"matches": []}})
        if path.endswith("/lineups"):
            return self.send(200, LINEUPS)
        if "/advanced" in path:
            return self.send(404, {"error": {"code": "ANALYTICS_UNAVAILABLE", "message": "never rated"}})
        if path.startswith("/v1/matches/"):
            return self.send(200, {"data": {"path": path, "players": [{"id": "p_9"}]}})
        if path.startswith("/v1/teams/") or path.startswith("/v1/players/"):
            return self.send(200, {"data": {"id": path.rsplit("/", 1)[1]}})
        return self.send(404, {"error": {"code": "RESOURCE_NOT_FOUND", "message": "?"}})


class HarvestEndToEndTests(SimpleTestCase):
    def setUp(self):
        FakeApi.calls, FakeApi.hit_429 = [], False
        self.server = HTTPServer(("127.0.0.1", 0), FakeApi)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "raw"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def run_cmd(self, *extra, key=KEY):
        out = StringIO()
        with mock.patch.dict("os.environ", {"PITCHAPI_API_KEY": key}), \
                mock.patch("ufd_app.pitchapi.get_api_key", return_value=key):
            call_command("harvest_pitchapi", "--output", str(self.out), "--base-url", self.url, "--pause", "0", *extra, stdout=out)
        return out.getvalue()

    def test_downloads_everything_and_records_what_is_unavailable(self):
        text = self.run_cmd()
        for rel in ("leagues.json", "league_l_PL.json", "matches/current.played.json", "match/m_1/lineups.json",
                    "match/m_2/stats.json", "match/m_1/events.json", "teams/t_A.json", "players/p_9.json", "_index.json"):
            self.assertTrue((self.out / rel).exists(), rel)
        advanced = json.loads((self.out / "match/m_1/advanced.json").read_text())
        self.assertEqual(advanced["_error"]["code"], "ANALYTICS_UNAVAILABLE")
        index = json.loads((self.out / "_index.json").read_text())
        self.assertEqual(index["rate_limited_429"], 1)
        self.assertEqual(index["by_endpoint"]["advanced"]["unavailable"], 2)
        self.assertEqual(index["failures"], [])
        self.assertNotIn(KEY, (self.out / "_index.json").read_text())
        self.assertIn("lineups", text)

    def test_second_run_is_resumable_and_makes_no_new_requests(self):
        self.run_cmd()
        FakeApi.calls.clear()
        text = self.run_cmd()
        self.assertEqual(FakeApi.calls, [])
        self.assertIn("already had", text)

    def test_max_requests_stops_cleanly_and_a_rerun_continues(self):
        text = self.run_cmd("--max-requests", "6")
        self.assertIn("Stopped early", text)
        first = len(list(self.out.rglob("*.json")))
        self.run_cmd()
        self.assertGreater(len(list(self.out.rglob("*.json"))), first)

    def test_endpoint_filter_and_zip(self):
        zip_path = Path(self.tmp.name) / "harvest.zip"
        self.run_cmd("--endpoints", "lineups", "--no-profiles", "--zip", str(zip_path))
        self.assertTrue((self.out / "match/m_1/lineups.json").exists())
        self.assertFalse((self.out / "match/m_1/stats.json").exists())
        self.assertFalse((self.out / "teams").exists())
        with zipfile.ZipFile(zip_path) as z:
            self.assertTrue(any(n.endswith("match/m_1/lineups.json") for n in z.namelist()))

    def test_explicit_seasons_are_requested(self):
        self.run_cmd("--seasons", "2025/2026", "--endpoints", "lineups", "--no-profiles")
        self.assertTrue(any("season=2025%2F2026" in c for c in FakeApi.calls))
        self.assertTrue((self.out / "matches/2025-2026.played.json").exists())

    def test_wrong_key_stops_immediately(self):
        with self.assertRaises(CommandError):
            self.run_cmd(key="pk_test_wrong")
        self.assertEqual(len([c for c in FakeApi.calls]), 1)

    def test_command_needs_a_key(self):
        with mock.patch("ufd_app.pitchapi.get_api_key", return_value=""), mock.patch.dict("os.environ", {}, clear=False):
            with self.assertRaises(CommandError):
                call_command("harvest_pitchapi", "--output", str(self.out), stdout=StringIO())
