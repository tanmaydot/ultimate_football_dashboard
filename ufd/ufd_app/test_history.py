import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

from . import history as hist
from . import pl_data
from .pl_fixtures import (AP_EVENTS, TODAY, FakeApiFootball, FakeFootballData, FakeFootballDataIo, IO_TEAMS)


def build(store, events=None, io=None, **kwargs):
    with mock.patch.object(pl_data, "MIN_TEAMS", 3):
        return pl_data.build_dataset(FakeFootballData(), FakeApiFootball(events=events), log=lambda *_: None,
                                     today=TODAY, history=store, io=io, **kwargs)


class HistoryStoreTests(SimpleTestCase):
    def test_trim_keeps_what_is_derived_and_drops_the_rest(self):
        event = dict(AP_EVENTS[0], big_blob="x" * 1000,
                     statistics=[{"type": "Shots Total", "home": "1", "away": "2"}, {"type": "Ball Possession", "home": "60%", "away": "40%"}])
        trimmed = hist.trim_event(event)
        self.assertNotIn("big_blob", trimmed)
        self.assertEqual(len(trimmed["statistics"]), 1)
        self.assertEqual(len(trimmed["lineup"]["home"]["starting_lineups"]), 11)

    def test_upsert_is_idempotent_and_a_finished_match_is_not_downgraded(self):
        store = hist.History(path=None)
        self.assertEqual(store.add_events(AP_EVENTS[:3]), (3, 0))
        self.assertEqual(store.add_events(AP_EVENTS[:3]), (0, 0))
        live = dict(AP_EVENTS[0], match_status="Live")
        self.assertEqual(store.add_events([live]), (0, 0))
        self.assertEqual(store.matches["m1"]["match_status"], "Finished")

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
            store = hist.History.load(path)
            store.add_events(AP_EVENTS[:2])
            store.save()
            self.assertEqual(len(hist.History.load(path).matches), 2)
            path.write_text("{ not json", encoding="utf-8")
            fresh = hist.History.load(path)
            self.assertEqual(fresh.matches, {})
            self.assertTrue(fresh.notes)
            self.assertTrue((Path(tmp) / "h.corrupt.json").exists())

    def test_new_season_archives_the_old_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "h.json"
            store = hist.History.load(path)
            store.start_season("2026-08-15")
            store.add_events(AP_EVENTS[:2])
            store.save()
            again = hist.History.load(path)
            again.start_season("2027-08-14")
            self.assertEqual(again.matches, {})
            self.assertTrue((Path(tmp) / "h.2026-08-15.json").exists())


class IncrementalBuildTests(SimpleTestCase):
    def test_history_keeps_matches_the_api_no_longer_returns(self):
        store = hist.History(path=None)
        first = build(store)
        self.assertEqual(first["teams"]["61"]["formation"]["matches_counted"], 3)
        # second run: the "free tier" now returns nothing at all, but the stored matches still count
        second = build(store, events=[])
        self.assertEqual(second["teams"]["61"]["formation"]["matches_counted"], 3)
        self.assertEqual(second["teams"]["61"]["formation"]["default"], "4-2-3-1")
        self.assertEqual(second["meta"]["history"]["matches_stored"], 4)  # includes the postponed one, which is not counted

    def test_second_run_only_asks_for_recent_days(self):
        store = hist.History(path=None)
        build(store)
        api = FakeApiFootball()
        with mock.patch.object(pl_data, "MIN_TEAMS", 3):
            pl_data.build_dataset(FakeFootballData(), api, log=lambda *_: None, today=TODAY, history=store)
        # newest stored match is 2026-09-20, so the window starts 3 days earlier, not at the season start
        self.assertEqual(api.event_windows[0][0], "2026-09-17")

    def test_new_match_is_appended_to_the_stored_ones(self):
        store = hist.History(path=None)
        build(store, events=AP_EVENTS[:3])
        newer = dict(AP_EVENTS[0], match_id="m9", match_date="2026-10-04", match_hometeam_system="4-4-2")
        result = build(store, events=[newer])
        counts = result["teams"]["61"]["formation"]["usage"]
        self.assertEqual(counts.get("4-4-2"), 1)
        self.assertEqual(result["teams"]["61"]["formation"]["matches_counted"], 4)

    def test_team_and_player_snapshots_are_recorded(self):
        store = hist.History(path=None)
        build(store)
        self.assertEqual(store.data["team_stats"]["61"][-1]["points"], 7)
        winger = store.data["player_stats"]["61"]["612"]
        self.assertEqual(winger["snapshots"][-1]["goals"], 4)

    def test_skip_events_still_uses_stored_matches(self):
        store = hist.History(path=None)
        build(store)
        result = build(store, include_events=False)
        self.assertEqual(result["teams"]["61"]["formation"]["matches_counted"], 3)

    def test_footballdata_io_stats_are_reused_within_the_max_age(self):
        store = hist.History(path=None)
        io = FakeFootballDataIo()
        now = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
        build(store, io=io, now=now)
        first_calls = len(io.stat_calls)
        self.assertGreater(first_calls, 0)
        io2 = FakeFootballDataIo()
        build(store, io=io2, now=datetime(2026, 10, 5, 20, tzinfo=timezone.utc))  # 8h later
        self.assertEqual([c for c in io2.stat_calls if c[0] != 903], [])  # 903 has no stats, so it is asked again
        io3 = FakeFootballDataIo()
        build(store, io=io3, now=datetime(2026, 10, 7, 12, tzinfo=timezone.utc))  # 2 days later: stale
        self.assertEqual(len(io3.stat_calls), first_calls)  # stale: everything is asked again

    def test_results_fall_back_to_stored_when_the_live_call_fails(self):
        store = hist.History(path=None)
        build(store)
        fd = FakeFootballData({"/competitions/PL/matches": pl_data.FetchError("rate limited")})
        with mock.patch.object(pl_data, "MIN_TEAMS", 3):
            result = pl_data.build_dataset(fd, FakeApiFootball(), log=lambda *_: None, today=TODAY, history=store)
        self.assertEqual(result["teams"]["61"]["stats"]["clean_sheets"], 2)
