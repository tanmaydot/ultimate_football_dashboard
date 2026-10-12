from unittest import mock

import requests
from django.test import SimpleTestCase, override_settings

from . import apifootball

NO_CACHE = {"default": {"BACKEND": "django.core.cache.backends.dummy.DummyCache"}}

# Shaped like the real get_leagues response (all values are strings).
APIFOOTBALL_LEAGUES = [
    {"country_id": "44", "country_name": "England", "league_id": "152", "league_name": "Premier League",
     "league_season": "2025/2026", "league_logo": "https://apiv3.apifootball.com/badges/logo_leagues/152_premier-league.png",
     "country_logo": "https://apiv3.apifootball.com/badges/logo_country/44_england.png"},
    {"country_id": "44", "country_name": "England", "league_id": "153", "league_name": "Championship",
     "league_season": "2025/2026", "league_logo": "", "country_logo": ""},
    {"country_id": "44", "country_name": "England", "league_id": "154", "league_name": "League One",
     "league_season": "2025/2026", "league_logo": "", "country_logo": ""},
]


def fake_response(payload, status=200):
    response = mock.Mock()
    response.status_code = status
    response.json.return_value = payload
    return response


@override_settings(CACHES=NO_CACHE, APIFOOTBALL_API_KEY="test-key")
class ApiFootballClientTests(SimpleTestCase):
    @mock.patch("ufd_app.apifootball.requests.get", return_value=fake_response(APIFOOTBALL_LEAGUES))
    def test_requests_england_leagues_with_the_key(self, mock_get):
        apifootball.get_england_leagues()
        args, kwargs = mock_get.call_args
        self.assertEqual(args[0], "https://apiv3.apifootball.com/")
        self.assertEqual(
            kwargs["params"],
            {"action": "get_leagues", "country_id": 44, "APIkey": "test-key"},
        )

    @mock.patch("ufd_app.apifootball.requests.get", return_value=fake_response(APIFOOTBALL_LEAGUES))
    def test_leagues_are_normalised_for_the_templates(self, _):
        leagues = apifootball.get_england_leagues()
        self.assertEqual([l["name"] for l in leagues], ["Premier League", "Championship", "League One"])
        pl = leagues[0]
        self.assertEqual(pl["apifootball_id"], "152")
        self.assertEqual(pl["area"]["name"], "England")
        self.assertEqual(pl["season"], "2025/2026")
        self.assertTrue(pl["emblem"].endswith("152_premier-league.png"))

    @mock.patch("ufd_app.apifootball.requests.get", return_value=fake_response(APIFOOTBALL_LEAGUES))
    def test_football_data_codes_are_bridged_where_known(self, _):
        codes = {l["name"]: l["code"] for l in apifootball.get_england_leagues()}
        self.assertEqual(codes, {"Premier League": "PL", "Championship": "ELC", "League One": None})

    @mock.patch("ufd_app.apifootball.requests.get")
    def test_missing_key_makes_no_request(self, mock_get):
        with override_settings(APIFOOTBALL_API_KEY=""), mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(apifootball.get_england_leagues(), [])
            self.assertFalse(apifootball.is_configured())
        mock_get.assert_not_called()

    @mock.patch("ufd_app.apifootball.requests.get", return_value=fake_response({"error": 404, "message": "No data"}))
    def test_error_object_gives_empty_list(self, _):
        self.assertEqual(apifootball.get_england_leagues(), [])

    @mock.patch("ufd_app.apifootball.requests.get", return_value=fake_response([], status=500))
    def test_http_error_gives_empty_list(self, _):
        self.assertEqual(apifootball.get_england_leagues(), [])

    @mock.patch("ufd_app.apifootball.requests.get", return_value=fake_response([{"league_name": "Premier League"}, "junk"]))
    def test_malformed_items_are_skipped_or_defaulted(self, _):
        leagues = apifootball.get_england_leagues()
        self.assertEqual(len(leagues), 1)
        self.assertEqual(leagues[0]["code"], "PL")

    @mock.patch("ufd_app.apifootball.requests.get",
                side_effect=requests.exceptions.ConnectionError("failed: ?APIkey=test-key&action=get_leagues"))
    def test_network_failure_never_logs_the_key(self, _):
        with self.assertLogs("ufd_app.apifootball", level="ERROR") as logs:
            self.assertEqual(apifootball.get_england_leagues(), [])
        self.assertNotIn("test-key", "\n".join(logs.output))

    @mock.patch("ufd_app.apifootball.requests.get", return_value=fake_response(APIFOOTBALL_LEAGUES))
    def test_result_is_cached(self, mock_get):
        locmem = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "step2-cache-test"}}
        with override_settings(CACHES=locmem):
            from django.core.cache import cache
            cache.clear()
            apifootball.get_england_leagues()
            apifootball.get_england_leagues()
            cache.clear()
        self.assertEqual(mock_get.call_count, 1)
