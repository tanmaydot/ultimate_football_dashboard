"""The committed data files must be sound, and the checker must catch a half-merged file."""
import json
import tempfile
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase

from .management.commands.check_data import DATA, check_files


def teams_file(count=20):
    return json.dumps({"meta": {"generated_at": "2026-10-19T00:00:00+00:00"}, "standings": [],
                       "teams": {str(i): {} for i in range(count)}})


HISTORY = json.dumps({"results": {}, "team_stats": {}, "player_stats": {}, "pitch_matches": {}})


class CommittedDataTests(SimpleTestCase):
    def test_the_committed_files_are_valid_and_free_of_conflict_markers(self):
        self.assertEqual(check_files(DATA / "pl_teams.json", DATA / "pl_history.json"), [])


class CheckerTests(SimpleTestCase):
    def check(self, teams, history):
        with tempfile.TemporaryDirectory() as folder:
            t, h = Path(folder) / "pl_teams.json", Path(folder) / "pl_history.json"
            t.write_text(teams, encoding="utf-8")
            h.write_text(history, encoding="utf-8")
            return check_files(t, h)

    def test_good_files_pass(self):
        self.assertEqual(self.check(teams_file(), HISTORY), [])

    def test_a_git_conflict_is_caught_even_if_the_rest_looks_like_json(self):
        broken = '{\n"meta": {"generated_at": "x"},\n<<<<<<< Updated upstream\n"teams": {}\n=======\n"teams": {}\n>>>>>>> Stashed changes\n}'
        problems = self.check(broken, HISTORY)
        self.assertTrue(any("conflict markers" in p for p in problems))
        problems = self.check(teams_file(), broken)
        self.assertTrue(any("pl_history.json" in p and "conflict markers" in p for p in problems))

    def test_truncated_json_is_caught(self):
        self.assertTrue(any("not valid JSON" in p for p in self.check(teams_file()[:40], HISTORY)))

    def test_too_few_clubs_is_caught(self):
        self.assertTrue(any("expected about 20" in p for p in self.check(teams_file(3), HISTORY)))

    def test_a_missing_file_is_caught(self):
        self.assertTrue(check_files("/no/such/teams.json", "/no/such/history.json"))

    def test_history_without_its_sections_is_caught(self):
        self.assertTrue(any("team_stats" in p for p in self.check(teams_file(), json.dumps({"results": {}}))))

    def test_the_command_fails_loudly_on_a_bad_file(self):
        with tempfile.TemporaryDirectory() as folder:
            t, h = Path(folder) / "t.json", Path(folder) / "h.json"
            t.write_text("{ not json", encoding="utf-8")
            h.write_text(HISTORY, encoding="utf-8")
            with self.assertRaises(CommandError):
                call_command("check_data", "--teams", str(t), "--history", str(h))
            t.write_text(teams_file(), encoding="utf-8")
            call_command("check_data", "--teams", str(t), "--history", str(h))
