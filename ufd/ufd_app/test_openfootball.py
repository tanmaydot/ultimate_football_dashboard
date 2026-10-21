"""OpenFootball parser, team-name handling and the cached archive."""
import tempfile
from datetime import date
from pathlib import Path

from django.test import SimpleTestCase

from . import openfootball as of
from .util import DATA_DIR

PLAIN = """= English Premier League 2023/24

▪ Matchday 1
Fri Aug 11 2023
  20:00   Burnley FC  0-3 (0-1)  Manchester City FC
Sat Aug 12
  12:30   Arsenal  2-1 (1-0)  Nottingham Forest
  (Saka 1-0 ; Havertz 2-0)
▪ Matchday 22
Sat Jan 13 2024
  15:00   Chelsea  1-0  Fulham
"""

WITH_V = """= English Premier League 2026/27
Matchday 7
Sat Oct 17 2026
  17:30  Arsenal FC            v Coventry City FC        3-0 (2-0)
  20:00  Brighton & Hove Albion FC  v  Hull City AFC
Matchday 8
Sat Oct 24
  12:30  Hull City AFC  v  Arsenal FC
"""


class ParserTests(SimpleTestCase):
    def test_reads_the_layout_without_v(self):
        matches = of.parse_season(PLAIN, 2023)
        self.assertEqual(len(matches), 3)
        first = matches[0]
        self.assertEqual((first["date"], first["matchday"], first["home"], first["away"], first["hg"], first["ag"]),
                         ("2023-08-11", 1, "burnley", "manchester city", 0, 3))
        self.assertEqual((matches[1]["home"], matches[1]["hg"], matches[1]["ag"]), ("arsenal", 2, 1))

    def test_dates_without_a_year_follow_the_season(self):
        matches = of.parse_season(PLAIN, 2023)
        self.assertEqual(matches[1]["date"], "2023-08-12")    # August to December: start year
        self.assertEqual(matches[2]["date"], "2024-01-13")    # explicit year wins
        late = of.parse_season("Sat Jun 06\n  15:00 A  1-0  B\n", 2019)
        self.assertEqual(late[0]["date"], "2020-06-06")        # June is the following calendar year

    def test_reads_the_layout_with_v_and_unplayed_fixtures(self):
        matches = of.parse_season(WITH_V, 2026)
        self.assertEqual(len(matches), 3)
        self.assertEqual((matches[0]["home"], matches[0]["away"], matches[0]["hg"], matches[0]["ag"]), ("arsenal", "coventry city", 3, 0))
        self.assertEqual((matches[1]["home"], matches[1]["away"]), ("brighton hove albion", "hull city"))
        self.assertIsNone(matches[1]["hg"])
        self.assertEqual((matches[2]["date"], matches[2]["matchday"]), ("2026-10-24", 8))

    def test_goal_scorer_lines_and_headers_are_not_matches(self):
        matches = of.parse_season(PLAIN, 2023)
        self.assertNotIn("saka", " ".join(m["home"] + m["away"] for m in matches))

    def test_empty_and_garbage_input(self):
        self.assertEqual(of.parse_season("", 2023), [])
        self.assertEqual(of.parse_season("nothing useful here\n1-0\n", 2023), [])
        self.assertEqual(of.parse_season("  20:00  A  1-0  B\n", 2023), [])     # a match before any date line is skipped

    def test_one_club_under_several_names(self):
        keys = {of.normalise_team_name(n) for n in ("Brighton & Hove Albion FC", "Brighton", "Brighton & Hove Albion")}
        self.assertEqual(len(keys), 1)

    def test_season_labels(self):
        self.assertEqual(of.season_label(1999), "1999-00")
        self.assertEqual(of.season_label(2026), "2026-27")
        self.assertEqual(of.current_season_start(date(2026, 10, 20)), 2026)
        self.assertEqual(of.current_season_start(date(2027, 3, 1)), 2026)


def full_season(year, teams=20):
    """A complete synthetic season: every ordered pairing once."""
    names = [f"club{i}" for i in range(teams)]
    return [{"date": f"{year}-09-01", "matchday": 1, "home": h, "away": a, "hg": 1, "ag": 0}
            for h in names for a in names if h != a]


class SanityTests(SimpleTestCase):
    def test_a_normal_season_has_no_problems(self):
        self.assertEqual(of.sanity_problems(full_season(2020)), [])

    def test_problems_are_named(self):
        self.assertTrue(any("clubs" in p for p in of.sanity_problems(full_season(2020, teams=19))))
        self.assertTrue(any("matches" in p for p in of.sanity_problems(full_season(2020)[:-1])))
        doubled = full_season(2020)
        doubled[0] = dict(doubled[1])
        self.assertTrue(any("twice" in p for p in of.sanity_problems(doubled)))


class ArchiveTests(SimpleTestCase):
    def test_save_and_load_round_trip(self):
        archive = of.Archive({"2020-21": full_season(2020)})
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "o.json"
            archive.save(path)
            loaded = of.Archive.load(path)
        self.assertEqual(loaded.seasons, archive.seasons)
        self.assertEqual(loaded.meta["source"], of.REPO_URL)

    def test_a_missing_or_broken_file_is_an_empty_archive(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(of.Archive.load(Path(folder) / "nope.json").seasons, {})
            broken = Path(folder) / "broken.json"
            broken.write_text("{not json", encoding="utf-8")
            self.assertEqual(of.Archive.load(broken).seasons, {})

    def test_update_downloads_only_what_is_missing_plus_the_current_season(self):
        asked = []

        def fetch(year):
            asked.append(year)
            return "\n".join(f"Sat Sep 01\n  15:00  {m['home']}  {m['hg']}-{m['ag']}  {m['away']}" for m in full_season(year))

        today = date(2002, 10, 1)       # current season 2002-03, so 2000-01 .. 2002-03 exist
        archive = of.Archive({"2000-01": full_season(2000)})
        fetched = archive.update(today=today, fetch=fetch)
        self.assertEqual(asked, [2001, 2002])
        self.assertEqual(fetched, ["2001-02", "2002-03"])
        asked.clear()
        archive.update(today=today, fetch=fetch)
        self.assertEqual(asked, [2002])                        # finished seasons are never downloaded twice
        asked.clear()
        archive.update(today=today, fetch=fetch, force=True)
        self.assertEqual(asked, [2000, 2001, 2002])

    def test_a_failed_download_keeps_the_earlier_copy_and_a_bad_season_is_rejected(self):
        def fetch(year):
            if year == 2000:
                raise OSError("offline")
            return "Sat Sep 01\n  15:00  A  1-0  B\n"            # one match: not a real season

        messages = []
        archive = of.Archive({"2000-01": full_season(2000)})
        archive.update(today=date(2002, 10, 1), fetch=fetch, force=True, log=messages.append)
        self.assertEqual(len(archive.seasons["2000-01"]), 380)       # the download failed: earlier copy kept
        self.assertNotIn("2001-02", archive.seasons)                 # finished season that is not a full season: rejected
        self.assertIn("2002-03", archive.seasons)                    # the season in progress is accepted as it stands
        self.assertTrue(any("not fetched" in m for m in messages))
        self.assertTrue(any("rejected" in m for m in messages))

    def test_played_and_fixtures(self):
        matches = [{"date": "2026-08-15", "matchday": 1, "home": "a", "away": "b", "hg": 2, "ag": 1},
                   {"date": "2026-08-22", "matchday": 2, "home": "b", "away": "a", "hg": None, "ag": None}]
        archive = of.Archive({"2026-27": matches})
        self.assertEqual(len(archive.played()), 1)
        self.assertEqual(archive.fixtures("2026-27")[0]["home"], "b")


class CommittedArchiveTests(SimpleTestCase):
    """The data file in the repository: every finished season must be complete, or the model is learning from gaps."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.archive = of.Archive.load(DATA_DIR / "openfootball.json")

    def test_it_covers_the_modern_premier_league(self):
        self.assertGreaterEqual(len(self.archive.seasons), 20)
        self.assertIn("2000-01", self.archive.seasons)

    def test_every_finished_season_is_complete(self):
        for label, matches in self.archive.seasons.items():
            if any(m["hg"] is None for m in matches):
                continue                                         # the season in progress
            self.assertEqual(of.sanity_problems(matches), [], label)

    def test_scores_are_plausible(self):
        goals = [m["hg"] + m["ag"] for m in self.archive.played()]
        self.assertTrue(all(0 <= g <= 14 for g in goals))
        self.assertAlmostEqual(sum(goals) / len(goals), 2.7, delta=0.3)     # the long-run Premier League average
