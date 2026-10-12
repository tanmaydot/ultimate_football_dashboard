import json
import os
import tempfile
from io import StringIO
from pathlib import Path
from unittest import mock

import requests
from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, override_settings

from .footballdata_io import FootballDataIoClient, FootballDataIoError
from .management.commands import probe_footballdata_io as probe

KEY = "sekrit-key-123"


def reply(status=200, body=None):
    r = mock.Mock()
    r.status_code = status
    r.json.return_value = body if body is not None else {"success": True, "data": {}}
    return r


def make_client(**kw):
    state = {"now": 0.0, "slept": []}

    def sleep(seconds):
        state["slept"].append(seconds)
        state["now"] += seconds

    client = FootballDataIoClient(api_key=KEY, sleep=sleep, clock=lambda: state["now"], **kw)
    return client, state


class ClientTests(SimpleTestCase):
    @mock.patch("ufd_app.footballdata_io.requests.get", return_value=reply(body={"success": True, "data": [1], "meta": {"plan": "free"}}))
    def test_sends_bearer_token_and_returns_the_envelope(self, get):
        client, _ = make_client()
        body = client.get("/leagues", {"page": 1})
        self.assertEqual(body["meta"]["plan"], "free")
        args, kwargs = get.call_args
        self.assertEqual(args[0], "https://footballdata.io/api/v1/leagues")
        self.assertEqual(kwargs["headers"]["Authorization"], f"Bearer {KEY}")
        self.assertEqual(kwargs["params"], {"page": 1})

    @mock.patch("ufd_app.footballdata_io.requests.get", return_value=reply())
    def test_calls_are_spaced(self, _):
        client, state = make_client(min_interval=0.5)
        client.get("/a")
        client.get("/b")
        self.assertEqual(state["slept"], [0.5])

    def test_documented_error_statuses_have_clear_messages(self):
        for status, text in ((401, "doesn't exist"), (403, "inactive"), (429, "monthly request limit")):
            with self.subTest(status=status), mock.patch(
                    "ufd_app.footballdata_io.requests.get",
                    return_value=reply(status, {"success": False, "error": {"code": "some_code", "message": "m"}})):
                client, _ = make_client()
                with self.assertRaises(FootballDataIoError) as ctx:
                    client.get("/x")
                self.assertIn(text, str(ctx.exception))
                self.assertEqual(ctx.exception.status, status)
                self.assertIn("[some_code]", str(ctx.exception))

    @mock.patch("ufd_app.footballdata_io.requests.get",
                return_value=reply(500, {"success": False, "error": {"code": "boom", "message": f"bad key {KEY} seen"}}))
    def test_error_text_from_the_api_is_scrubbed_of_the_key(self, _):
        client, _ = make_client()
        with self.assertRaises(FootballDataIoError) as ctx:
            client.get("/x")
        self.assertNotIn(KEY, str(ctx.exception))

    @mock.patch("ufd_app.footballdata_io.requests.get", return_value=reply(200, {"success": False, "error": {"code": "x", "message": "nope"}}))
    def test_success_false_is_an_error_even_with_http_200(self, _):
        with self.assertRaises(FootballDataIoError):
            make_client()[0].get("/x")

    @mock.patch("ufd_app.footballdata_io.requests.get")
    def test_non_json_response_is_an_error(self, get):
        bad = reply(502)
        bad.json.side_effect = ValueError
        get.return_value = bad
        with self.assertRaises(FootballDataIoError) as ctx:
            make_client()[0].get("/x")
        self.assertIn("HTTP 502", str(ctx.exception))

    @mock.patch("ufd_app.footballdata_io.requests.get",
                side_effect=requests.exceptions.ConnectionError(f"url had {KEY} in it"))
    def test_network_errors_never_echo_exception_text(self, _):
        with self.assertRaises(FootballDataIoError) as ctx:
            make_client()[0].get("/x")
        self.assertNotIn(KEY, str(ctx.exception))

    def test_missing_key_means_no_request(self):
        with override_settings(FOOTBALLDATA_IO_API_KEY=""), mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch("ufd_app.footballdata_io.requests.get") as get:
            client = FootballDataIoClient()
            self.assertFalse(client.is_configured())
            with self.assertRaises(FootballDataIoError):
                client.get("/x")
        get.assert_not_called()

    def test_key_is_read_from_settings(self):
        with override_settings(FOOTBALLDATA_IO_API_KEY="from-settings"):
            self.assertEqual(FootballDataIoClient().api_key, "from-settings")


class PaginationTests(SimpleTestCase):
    def pages(self, total_pages, per_page=2):
        def fake(url, headers, params, timeout):
            page = params["page"]
            return reply(body={"success": True, "data": [f"p{page}-{i}" for i in range(per_page)],
                               "pagination": {"page": page, "limit": params["limit"], "total_pages": total_pages}})
        return fake

    def test_follows_every_page(self):
        with mock.patch("ufd_app.footballdata_io.requests.get", side_effect=self.pages(3)) as get:
            items = make_client()[0].get_all("/teams")
        self.assertEqual(len(items), 6)
        self.assertEqual(get.call_count, 3)

    def test_max_pages_protects_the_monthly_allowance(self):
        with mock.patch("ufd_app.footballdata_io.requests.get", side_effect=self.pages(500)) as get:
            make_client()[0].get_all("/teams", max_pages=4)
        self.assertEqual(get.call_count, 4)

    def test_single_non_list_payload_is_returned_as_is(self):
        with mock.patch("ufd_app.footballdata_io.requests.get", return_value=reply(body={"success": True, "data": {"a": 1}})):
            self.assertEqual(make_client()[0].get_all("/x"), {"a": 1})


class ProbeHelperTests(SimpleTestCase):
    def test_trim_cuts_long_lists_and_says_so(self):
        out = probe.trim({"rows": list(range(10)), "ok": 1})
        self.assertEqual(out["rows"][:3], [0, 1, 2])
        self.assertIn("7 more items", out["rows"][3])

    def test_items_of_handles_lists_and_wrapped_lists(self):
        self.assertEqual(probe.items_of([{"a": 1}, "x"]), [{"a": 1}])
        self.assertEqual(probe.items_of({"leagues": [{"a": 1}]}), [{"a": 1}])
        self.assertEqual(probe.items_of(None), [])

    def test_premier_league_is_found_without_knowing_field_names(self):
        items = [{"x": "Championship", "k": 1}, {"label": "English Premier League", "slug": "epl"}]
        found = probe.find_by_text(items, "premier league")
        self.assertEqual(probe.pick_id(found), ("slug", "epl"))
        self.assertEqual(probe.pick_id({"nothing": 1}), (None, None))


class FakeApi:
    """Answers like footballdata.io might; field names are invented on purpose."""

    def __init__(self, fail=None):
        self.fail = fail or {}
        self.paths = []

    def is_configured(self):
        return True

    def get(self, path, params=None):
        self.paths.append(path)
        if path in self.fail:
            raise self.fail[path]
        data = {
            "/countries/england/leagues": [{"title": "National League"}, {"title": "Premier League", "league_id": 77}],
            "/leagues/77/teams": [{"team_id": 5, "title": f"Team {i}"} for i in range(20)],
            "/leagues/77/matches": [{"match_id": 900}],
        }.get(path, {"ok": path})
        return {"success": True, "data": data, "meta": {"plan": "free", "requests_used": 3}}


class ProbeCommandTests(SimpleTestCase):
    def run_probe(self, api, *args):
        out = StringIO()
        with tempfile.TemporaryDirectory() as folder, mock.patch(
                "ufd_app.management.commands.probe_footballdata_io.FootballDataIoClient", return_value=api):
            target = Path(folder) / "probe.json"
            call_command("probe_footballdata_io", "--output", str(target), *args, stdout=out)
            return json.loads(target.read_text(encoding="utf-8")), out.getvalue()

    def test_walks_league_team_and_match_and_saves_trimmed_responses(self):
        api = FakeApi()
        saved, output = self.run_probe(api)
        self.assertEqual(saved["premier_league_identified_by"], {"field": "league_id", "value": 77})
        for path in ("/leagues/77/standings", "/teams/5", "/teams/5/players", "/teams/5/stats", "/matches/900/stats"):
            self.assertIn(path, api.paths)
        teams_entry = next(r for r in saved["requests"] if r["path"] == "/leagues/77/teams")
        self.assertEqual(len(teams_entry["response"]["data"]), 4)  # 3 samples + the "more items" note
        self.assertIn("Saved", output)

    def test_saved_file_never_contains_the_api_key(self):
        api = FakeApi()
        api.api_key = "sekrit"
        saved, _ = self.run_probe(api)
        self.assertNotIn("sekrit", json.dumps(saved))

    def test_unknown_league_is_reported_with_a_way_forward(self):
        api = FakeApi()
        api.get = lambda path, params=None: {"success": True, "data": [{"title": "Other"}]}
        saved, output = self.run_probe(api)
        self.assertIsNone(saved["premier_league_identified_by"]["value"])
        self.assertIn("--league-id", output)

    def test_league_id_option_skips_the_search(self):
        api = FakeApi()
        saved, _ = self.run_probe(api, "--league-id", "77")
        self.assertEqual(saved["premier_league_identified_by"]["field"], "given")

    def test_individual_failures_are_recorded_and_the_rest_continue(self):
        api = FakeApi(fail={"/teams/5/stats": FootballDataIoError("footballdata.io /teams/5/stats: HTTP 404", status=404)})
        saved, _ = self.run_probe(api)
        failed = [r for r in saved["requests"] if not r["ok"]]
        self.assertEqual([r["path"] for r in failed], ["/teams/5/stats"])
        self.assertIn("/matches/900/stats", api.paths)

    def test_a_bad_key_stops_immediately(self):
        api = FakeApi(fail={"/meta/status": FootballDataIoError("footballdata.io: the API key doesn't exist", status=401)})
        with self.assertRaises(CommandError):
            self.run_probe(api)
        self.assertEqual(api.paths, ["/meta/status"])

    def test_missing_key_fails_clearly(self):
        api = FakeApi()
        api.is_configured = lambda: False
        with self.assertRaises(CommandError):
            self.run_probe(api)


class SourceTests(SimpleTestCase):
    class FakeClient:
        def __init__(self, bodies):
            self.bodies, self.calls = bodies, []

        def is_configured(self):
            return True

        def get(self, path, params=None):
            self.calls.append((path, params))
            return self.bodies[path]

    def source(self, bodies):
        from .footballdata_io import FootballDataIoSource
        return FootballDataIoSource(self.FakeClient(bodies))

    def test_premier_league_is_found_by_competition_name(self):
        src = self.source({"/countries/england/leagues": {"data": {"leagues": [
            {"league_id": 16, "competition_name": "Championship"},
            {"league_id": 15, "competition_name": "Premier League"}]}}})
        self.assertEqual(src.premier_league_id(), 15)

    def test_current_teams_keep_only_latest_season_once_each(self):
        from .pl_fixtures import IO_SEASON_ID, IO_TEAMS
        src = self.source({"/leagues/15/teams": {"data": {"teams": IO_TEAMS}}})
        season_id, teams = src.get_current_teams(15)
        self.assertEqual(season_id, IO_SEASON_ID)
        self.assertEqual([t["team_id"] for t in teams], [901, 902, 903])

    def test_stats_for_another_competition_are_rejected(self):
        from .pl_fixtures import io_stats
        wrong = io_stats(57, league_id=45, season_id=90425)
        src = self.source({"/teams/57/stats": {"data": wrong}})
        self.assertIsNone(src.get_team_stats(57, 15, 103535))
        self.assertEqual(src.client.calls[0][1], {"league_id": 15, "season_id": 103535})

    def test_clean_name_unescapes(self):
        from .footballdata_io import clean_name
        self.assertEqual(clean_name("Brighton &amp; Hove Albion"), "Brighton & Hove Albion")

    def test_pagination_is_read_from_meta(self):
        def fake(url, headers, params, timeout):
            page = params["page"]
            return reply(body={"success": True, "data": [page],
                               "meta": {"pagination": {"total_pages": 2}}})
        with mock.patch("ufd_app.footballdata_io.requests.get", side_effect=fake) as get:
            self.assertEqual(make_client()[0].get_all("/x"), [1, 2])
        self.assertEqual(get.call_count, 2)
