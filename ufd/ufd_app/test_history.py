import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

from . import history as hist
from . import pitchapi_source, pl_data
from .pl_fixtures import (FakeFootballData, FakeFootballDataIo, MUN, SimpleSource, TODAY, WOLVES, full_source, io_stats,
                          pitch_match)


def build(store, pa=None, io=None, **kwargs):
    with mock.patch.object(pl_data, "MIN_TEAMS", 3):
        return pl_data.build_dataset(FakeFootballData(), pa=pa, log=lambda *_: None, today=TODAY, history=store, io=io, **kwargs)


class HistoryStoreTests(SimpleTestCase):
    def stored(self):
        store = hist.History(path=None)
        pitchapi_source.refresh_history(full_source(), store, today=TODAY, log=lambda *_: None)
        return store

    def test_pitch_matches_are_stored_once_and_player_details_kept_separately(self):
        store = self.stored()
        self.assertEqual(sorted(store.pitch_matches), ["m_1", "m_2", "m_3"])
        self.assertNotIn("info", store.pitch_match("m_1"))
        self.assertEqual(store.data["pitch_players"]["p_t_chew"]["name"], "Chelsea Winger")
        self.assertEqual(store.last_match_date(), "2026-09-26")
        self.assertEqual([r["event"]["match_date"] for r in store.pitch_records()], ["2026-08-22", "2026-09-12", "2026-09-26"])

    def test_snapshots_only_when_something_changed_and_same_day_replaces(self):
        store = hist.History(path=None)
        self.assertTrue(store.record_team(61, "2026-10-01", {"points": 3, "played": 1}))
        self.assertFalse(store.record_team(61, "2026-10-02", {"points": 3, "played": 1}))
        self.assertTrue(store.record_team(61, "2026-10-03", {"points": 6, "played": 2}))
        self.assertTrue(store.record_team(61, "2026-10-03", {"points": 7, "played": 2}))  # same day: replaced
        self.assertEqual([s["points"] for s in store.data["team_stats"]["61"]], [3, 7])
        self.assertTrue(store.record_player(61, 611, "A", "2026-10-03", {"goals": 1}))
        self.assertFalse(store.record_player(61, 611, "A", "2026-10-04", {"goals": 1}))

    def test_save_load_roundtrip_and_corrupt_file_is_backed_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "h.json"
            store = self.stored()
            store.path = path
            store.save()
            self.assertEqual(len(hist.History.load(path).pitch_matches), 3)
            path.write_text("{ not json", encoding="utf-8")
            fresh = hist.History.load(path)
            self.assertEqual(fresh.pitch_matches, {})
            self.assertTrue(fresh.notes)
            self.assertTrue((Path(tmp) / "h.corrupt.json").exists())

    def test_a_git_conflict_in_the_file_is_treated_as_corrupt_not_trusted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "h.json"
            path.write_text('{\n  "version": 1,\n<<<<<<< Updated upstream\n  "updated_at": "a"\n=======\n  "updated_at": "b"\n>>>>>>> Stashed changes\n}', encoding="utf-8")
            self.assertTrue(hist.History.load(path).notes)

    def test_new_season_archives_the_old_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "h.json"
            store = self.stored()
            store.path = path
            store.start_season("2026-08-15")
            store.save()
            again = hist.History.load(path)
            again.start_season("2027-08-14")
            self.assertEqual(again.pitch_matches, {})
            self.assertTrue((Path(tmp) / "h.2026-08-15.json").exists())

    def test_a_file_written_by_the_old_version_loads_and_its_match_section_is_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "h.json"
            path.write_text(json.dumps({"version": 1, "season_start": "2026-08-15", "matches": {"812650": {"match_id": "812650"}},
                                        "results": {"1": {"date": "2026-08-21"}}, "team_stats": {"61": [{"date": "2026-10-01", "points": 3}]},
                                        "player_stats": {}, "io_stats": {}}), encoding="utf-8")
            store = hist.History.load(path)
            self.assertNotIn("matches", store.data)
            self.assertEqual(store.data["team_stats"]["61"][0]["points"], 3)          # the season's snapshots are kept
            self.assertEqual(store.pitch_matches, {})
            self.assertTrue(any("older version" in n for n in store.notes))
            store.path = path
            store.save()
            self.assertNotIn("matches", json.loads(path.read_text()))

    def test_io_stats_are_stale_once_the_club_has_played_again(self):
        store = hist.History(path=None)
        now = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
        store.set_io(61, {"x": 1}, now, played=3)
        self.assertEqual(store.io_cached(61, 24, now, played=3), {"x": 1})
        self.assertIsNone(store.io_cached(61, 24, now, played=4))                    # it played: stale
        self.assertEqual(store.io_cached(61, 24, now), {"x": 1})                     # played not known: age decides
        self.assertIsNone(store.io_cached(61, 24, now + timedelta(hours=25), played=3))

    def test_io_stats_stored_by_the_old_version_without_a_played_count_still_count_by_age(self):
        store = hist.History(path=None)
        now = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
        store.data["io_stats"]["61"] = {"fetched_at": now.isoformat(), "data": {"x": 1}}
        self.assertEqual(store.io_cached(61, 24, now, played=9), {"x": 1})

    def test_io_directory_is_remembered_for_a_while(self):
        store = hist.History(path=None)
        now = datetime(2026, 10, 5, tzinfo=timezone.utc)
        self.assertIsNone(store.io_directory(168, now))
        store.set_io_directory(15, 103535, [{"team_id": 901}], now)
        self.assertEqual(store.io_directory(168, now + timedelta(days=3))["season_id"], 103535)
        self.assertIsNone(store.io_directory(168, now + timedelta(days=8)))


class IncrementalBuildTests(SimpleTestCase):
    def test_history_keeps_matches_the_api_no_longer_lists(self):
        store = hist.History(path=None)
        first = build(store, full_source())
        self.assertEqual(first["teams"]["61"]["formation"]["matches_counted"], 3)
        # second run: the API now lists nothing at all, but the stored matches still count
        second = build(store, SimpleSource([], {}))
        self.assertEqual(second["teams"]["61"]["formation"]["matches_counted"], 3)
        self.assertEqual(second["teams"]["61"]["formation"]["default"], "4-2-3-1")
        self.assertEqual(second["meta"]["history"]["pitch_matches_stored"], 3)

    def test_new_match_is_appended_to_the_stored_ones(self):
        store = hist.History(path=None)
        build(store, full_source())
        newer = full_source([pitch_match("m_9", MUN, ("t_che", "Chelsea"), "2026-10-04", (0, 1))])
        newer.bundles["m_9"]["lineups"] = dict(newer.bundles["m_9"]["lineups"],
                                               away=dict(newer.bundles["m_9"]["lineups"]["away"], formation="4-4-2"))
        result = build(store, newer)
        counts = result["teams"]["61"]["formation"]["usage"]
        self.assertEqual(counts.get("4-4-2"), 1)
        self.assertEqual(result["teams"]["61"]["formation"]["matches_counted"], 4)
        self.assertEqual(newer.fetched, ["m_9"])

    def test_team_and_player_snapshots_are_recorded(self):
        store = hist.History(path=None)
        build(store, full_source())
        self.assertEqual(store.data["team_stats"]["61"][-1]["points"], 7)
        winger = store.data["player_stats"]["61"]["612"]
        self.assertEqual(winger["snapshots"][-1]["goals"], 3)
        self.assertEqual(winger["snapshots"][-1]["assists"], 3)

    def test_footballdata_io_stats_are_reused_within_the_max_age(self):
        store = hist.History(path=None)
        stats = {901: io_stats(901), 902: io_stats(902), 903: io_stats(903)}
        io = FakeFootballDataIo(stats=stats)
        now = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
        build(store, io=io, now=now)
        self.assertEqual(len(io.stat_calls), 3)
        io2 = FakeFootballDataIo(stats=stats)
        build(store, io=io2, now=datetime(2026, 10, 5, 20, tzinfo=timezone.utc))  # 8h later, nobody played
        self.assertEqual(io2.stat_calls, [])
        io3 = FakeFootballDataIo(stats=stats)
        build(store, io=io3, now=datetime(2026, 10, 7, 12, tzinfo=timezone.utc))  # 2 days later: stale
        self.assertEqual(len(io3.stat_calls), 3)

    def test_results_fall_back_to_stored_when_the_live_call_fails(self):
        store = hist.History(path=None)
        build(store, full_source())
        fd = FakeFootballData({"/competitions/PL/matches": pl_data.FetchError("rate limited")})
        with mock.patch.object(pl_data, "MIN_TEAMS", 3):
            result = pl_data.build_dataset(fd, pa=SimpleSource([], {}), log=lambda *_: None, today=TODAY, history=store)
        self.assertEqual(result["teams"]["61"]["stats"]["clean_sheets"], 2)
