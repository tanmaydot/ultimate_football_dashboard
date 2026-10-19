"""Check that the generated data files are sound. Used by the daily workflow before it commits, and by the tests.

A file that is half-merged (git conflict markers), cut short, or has far too few clubs must never be committed:
the dashboard would break on Render, and the next build would throw the season's history away.
"""
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

DATA = Path(__file__).resolve().parents[2] / "data"


def check_files(teams_path=DATA / "pl_teams.json", history_path=DATA / "pl_history.json", min_teams=18):
    """Return a list of problems (empty when everything is fine)."""
    problems = []
    for path in (Path(teams_path), Path(history_path)):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            problems.append(f"{path.name}: can't be read ({exc})")
            continue
        if any(line.startswith(("<<<<<<<", ">>>>>>>")) or line == "=======" for line in text.splitlines()):
            problems.append(f"{path.name}: has git conflict markers (<<<<<<< / ======= / >>>>>>>)")
            continue
        try:
            data = json.loads(text)
        except ValueError as exc:
            problems.append(f"{path.name}: is not valid JSON ({exc})")
            continue
        if path.name == Path(teams_path).name:
            teams = data.get("teams") if isinstance(data, dict) else None
            if not isinstance(teams, dict) or len(teams) < min_teams:
                problems.append(f"{path.name}: has {len(teams) if isinstance(teams, dict) else 0} teams, expected about 20")
            if not (data.get("meta") or {}).get("generated_at"):
                problems.append(f"{path.name}: has no meta.generated_at")
        else:
            for key in ("results", "team_stats", "player_stats", "pitch_matches"):
                if not isinstance(data.get(key), dict):
                    problems.append(f"{path.name}: has no '{key}' section")
    return problems


class Command(BaseCommand):
    help = "Check pl_teams.json and pl_history.json are valid (no conflict markers, enough clubs). Exit code 1 if not."

    def add_arguments(self, parser):
        parser.add_argument("--teams", default=str(DATA / "pl_teams.json"))
        parser.add_argument("--history", default=str(DATA / "pl_history.json"))

    def handle(self, *args, **options):
        problems = check_files(options["teams"], options["history"])
        if problems:
            raise CommandError("The data files are not fit to commit:\n  " + "\n  ".join(problems))
        self.stdout.write(self.style.SUCCESS("Data files are valid."))
