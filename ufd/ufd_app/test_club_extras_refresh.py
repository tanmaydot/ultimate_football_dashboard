import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase, TestCase

from . import club_extras_refresh as cer
from . import dashboard
from .test_dashboard import built_data


class ParsingTests(SimpleTestCase):
    def test_city_loses_the_uk_postcode(self):
        self.assertEqual(cer.clean_city("N5 1BU London"), "London")
        self.assertEqual(cer.clean_city("NG2 5FJ West Bridgford"), "West Bridgford")
        self.assertEqual(cer.clean_city("BH7 7AF   Bournemouth"), "Bournemouth")
        self.assertEqual(cer.clean_city("M16 ORA Manchester"), "Manchester")  # the API has a letter O here
        self.assertEqual(cer.clean_city("Liverpool"), "Liverpool")
        self.assertIsNone(cer.clean_city(None))

    def test_transfermarkt_wins_stadium_and_disagreement_is_flagged(self):
        tm = {"venue": "Villa Park", "capacity": 36887, "opened": 1897, "city": "Birmingham"}
        wd = {"venue": "Villa Park", "capacity": 42640, "opened": 1896, "city": "Birmingham"}
        stadium, note = cer.choose_stadium(tm, wd)
        self.assertEqual(stadium, {"name": "Villa Park", "capacity": 36887, "city": "Birmingham", "opened": 1897})
        self.assertIn("36,887", note)
        self.assertIn("42,640", note)

    def test_close_capacities_are_not_flagged(self):
        tm = {"venue": "Stamford Bridge", "capacity": 40044, "opened": 1877, "city": "London"}
        wd = {"venue": "Stamford Bridge", "capacity": 41875, "opened": 1877, "city": "Fulham"}
        stadium, note = cer.choose_stadium(tm, wd)
        self.assertIsNone(note)
        self.assertEqual(stadium["capacity"], 40044)
        self.assertEqual(stadium["city"], "London")  # never Wikidata's council area

    def test_wikidata_only_fills_gaps_and_never_a_different_grounds_year(self):
        # Wikidata still lists Griffin Park for Brentford; its year must not leak into the new ground.
        tm = {"venue": "Gtech Community Stadium", "capacity": 17250, "opened": None, "city": "London"}
        wd = {"venue": "Griffin Park", "capacity": 14863, "opened": 1904, "city": None}
        stadium, _ = cer.choose_stadium(tm, wd)
        self.assertEqual(stadium, {"name": "Gtech Community Stadium", "capacity": 17250, "city": "London"})
        stadium, _ = cer.choose_stadium(None, {"venue": "X", "capacity": 20000, "opened": 1950})
        self.assertEqual(stadium, {"capacity": 20000, "opened": 1950})

    def test_honours_merge_old_and_new_european_cup_and_skip_the_rest(self):
        ach = [
            {"title": "UEFA Champions League winner", "count": 2},
            {"title": "European Champion Clubs' Cup winner", "count": 4},
            {"title": "English Champion", "count": 20},
            {"title": "English 3rd tier champion", "count": 4},   # deliberately hidden
            {"title": "", "count": 15},                           # untitled group from the API
            {"title": "Some New Trophy", "count": 1},             # unknown: reported, not shown
            {"title": "FA Cup Winner", "count": 8},
        ]
        honours, unmapped = cer.map_honours(ach)
        self.assertEqual(honours, [
            {"name": "English League Champions", "count": 20},
            {"name": "FA Cup", "count": 8},
            {"name": "European Cup / Champions League", "count": 6},
        ])
        self.assertEqual(unmapped, ["Some New Trophy"])

    def test_every_club_id_is_a_string_pair(self):
        self.assertEqual(len(cer.CLUB_IDS), 20)
        for tid, ids in cer.CLUB_IDS.items():
            self.assertTrue(tid.isdigit() and ids["tm"].isdigit() and ids["wd"].startswith("Q"), tid)


class FakeFetch:
    def __init__(self, fail_tm_for=()):
        self.fail_tm_for = fail_tm_for

    def get_json(self, url, params=None, retries=3):
        if "sparql" in url:
            return {"results": {"bindings": [
                {"venueLabel": {"value": "Emirates Stadium"}, "cap": {"value": "60338"}, "inc": {"value": "2006-07-22T00:00:00Z"}}]}}
        club = url.split("/clubs/")[1].split("/")[0]
        if club in self.fail_tm_for:
            raise RuntimeError("API down")
        if url.endswith("/profile"):
            return {"stadium": {"name": "Emirates Stadium", "capacity": 60704, "buildYear": 2006, "addressLine2": "N5 1BU London"}}
        return {"achievements": [{"title": "FA Cup Winner", "count": 14}]}


class RefreshAllTests(SimpleTestCase):
    NOW = datetime(2026, 10, 13, tzinfo=timezone.utc)

    def test_writes_stadium_and_honours(self):
        result, summary = cer.refresh_all(FakeFetch(), ["57"], {}, now=self.NOW)
        self.assertEqual(summary["ok"], ["57"])
        self.assertEqual(result["57"]["stadium"], {"name": "Emirates Stadium", "capacity": 60704, "city": "London", "opened": 2006})
        self.assertEqual(result["57"]["honours"], [{"name": "FA Cup", "count": 14}])
        self.assertEqual(result["57"]["_updated"], "2026-10-13")
        self.assertIn("_readme", result)

    def test_a_failing_club_keeps_its_previous_entry(self):
        existing = {"57": {"stadium": {"capacity": 1}}, "_readme": "old"}
        # Transfermarkt down; the Wikidata fallback still answers, so the entry is rebuilt from it only.
        result, summary = cer.refresh_all(FakeFetch(fail_tm_for={"11"}), ["57"], existing, now=self.NOW)
        self.assertEqual(result["57"]["stadium"]["capacity"], 60338)
        self.assertTrue(any("partial" in n for n in result["57"]["_review"]))

        class AllDown(FakeFetch):
            def get_json(self, *a, **k):
                raise RuntimeError("everything is down")
        result, summary = cer.refresh_all(AllDown(), ["57"], existing, now=self.NOW)
        self.assertEqual(result["57"], {"stadium": {"capacity": 1}})  # untouched
        self.assertIn("57", summary["failed"])

    def test_unknown_club_is_skipped_and_reported(self):
        result, summary = cer.refresh_all(FakeFetch(), ["9999"], {}, now=self.NOW)
        self.assertEqual(summary["skipped_no_ids"], ["9999"])
        self.assertNotIn("9999", result)


class MergeAndLoaderTests(TestCase):
    def test_manual_wins_per_field_and_stadium_merges_by_key(self):
        auto = {"57": {"stadium": {"capacity": 36887, "city": "Birmingham", "opened": 1897},
                       "honours": [{"name": "FA Cup", "count": 7}]}}
        manual = {"57": {"stadium": {"capacity": 42640}, "revenue": {"amount_m": 1}},
                  "61": {"honours": [{"name": "X", "count": 1}]}}
        merged = dashboard.merge_extras(auto, manual)
        self.assertEqual(merged["57"]["stadium"], {"capacity": 42640, "city": "Birmingham", "opened": 1897})
        self.assertEqual(merged["57"]["honours"], [{"name": "FA Cup", "count": 7}])
        self.assertEqual(merged["57"]["revenue"], {"amount_m": 1})
        self.assertEqual(merged["61"]["honours"], [{"name": "X", "count": 1}])

    def test_manual_honours_replace_the_generated_list_whole(self):
        merged = dashboard.merge_extras({"57": {"honours": [{"name": "A", "count": 1}, {"name": "B", "count": 2}]}},
                                        {"57": {"honours": [{"name": "C", "count": 3}]}})
        self.assertEqual(merged["57"]["honours"], [{"name": "C", "count": 3}])

    def test_loader_reads_both_files_and_ignores_comment_keys_and_bad_json(self):
        with tempfile.TemporaryDirectory() as folder:
            auto, manual = Path(folder) / "auto.json", Path(folder) / "manual.json"
            auto.write_text(json.dumps({"_readme": "x", "61": {"stadium": {"capacity": 40000}}}), encoding="utf-8")
            manual.write_text("{ this is not json", encoding="utf-8")
            with mock.patch.object(dashboard, "AUTO_EXTRAS_PATH", auto), mock.patch.object(dashboard, "EXTRAS_PATH", manual):
                self.assertEqual(dashboard.load_club_extras(), {"61": {"stadium": {"capacity": 40000}}})
            with mock.patch.object(dashboard, "AUTO_EXTRAS_PATH", Path(folder) / "missing.json"), \
                    mock.patch.object(dashboard, "EXTRAS_PATH", Path(folder) / "missing2.json"):
                self.assertEqual(dashboard.load_club_extras(), {})

    def test_capacity_rank_works_from_the_extras_when_the_api_file_has_none(self):
        data = built_data()
        for team in data["teams"].values():
            team["profile"]["venue"]["capacity"] = None  # what the real pl_teams.json looks like
        ids = list(data["teams"])
        extras = {ids[0]: {"stadium": {"capacity": 60000}}, ids[1]: {"stadium": {"capacity": 30000}},
                  ids[2]: {"stadium": {"capacity": 45000}}}
        ctx, _ = dashboard.build_dashboard_context(int(ids[2]), data=data, extras_by_team=extras)
        self.assertEqual(ctx["stadium"]["capacity_rank"], "2nd largest in the league")
        self.assertEqual(ctx["stadium"]["capacity_biggest"], "60,000")
        self.assertEqual(ctx["stadium"]["capacity_pct"], 75)


class DefaultLayoutTests(SimpleTestCase):
    def test_trophies_live_in_team_info_and_the_widgets_do_not_overlap_when_added(self):
        by_id = {w["id"]: w for w in dashboard.DEFAULT_LAYOUT}
        self.assertNotIn("trophies", by_id)  # no separate widget: the cabinet is part of Team info
        self.assertTrue(by_id["revenue"]["hidden"] and by_id["coaches"]["hidden"])
        row = sorted((by_id[i]["x"], by_id[i]["x"] + by_id[i]["w"]) for i in ("revenue", "coaches"))
        for (_, end), (start, _) in zip(row, row[1:]):
            self.assertLessEqual(end, start)  # side by side, 12-column grid
        self.assertLessEqual(row[-1][1], 12)


class StadiumNameTests(TestCase):
    def test_extras_name_replaces_a_stale_api_ground_name(self):
        data = built_data()
        tid = next(iter(data["teams"]))
        data["teams"][tid]["profile"]["venue"]["name"] = "Griffin Park"
        extras = {tid: {"stadium": {"name": "Gtech Community Stadium", "capacity": 17250}}}
        ctx, _ = dashboard.build_dashboard_context(int(tid), data=data, extras_by_team=extras)
        self.assertEqual(ctx["stadium"]["name"], "Gtech Community Stadium")
        ctx, _ = dashboard.build_dashboard_context(int(tid), data=data, extras_by_team={})
        self.assertEqual(ctx["stadium"]["name"], "Griffin Park")  # no extras: the API name, as before