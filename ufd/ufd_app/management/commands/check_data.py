"""Check that the generated data files are sound. Used by the daily workflow before it commits, and by the tests.

A file that is half-merged (git conflict markers), cut short, or has far too few clubs must never be committed:
the dashboard would break on Render, and the next build would throw the season's history away.
"""
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

DATA = Path(__file__).resolve().parents[2] / "data"


def check_files(teams_path=DATA / "pl_teams.json", history_path=DATA / "pl_history.json", min_teams=18,
                predictions_path=None, archive_path=None):
    """Return a list of problems (empty when everything is fine). The predictions and OpenFootball files are
    checked only when a path is given and the file exists (they appear after the first build_predictions)."""
    problems = []
    optional = [Path(p) for p in (predictions_path, archive_path) if p and Path(p).exists()]
    for path in (Path(teams_path), Path(history_path), *optional):
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
        if path in optional:
            problems.extend(_check_derived(path, data, archive_path))
        elif path.name == Path(teams_path).name:
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


def _check_derived(path, data, archive_path):
    """Problems in pl_predictions.json (probabilities that do not add up) or openfootball.json (an incomplete season)."""
    if archive_path and path == Path(archive_path):
        seasons = data.get("seasons") if isinstance(data, dict) else None
        if not isinstance(seasons, dict) or not seasons:
            return [f"{path.name}: has no seasons"]
        finished = [label for label, rows in seasons.items() if all(r[4] is not None for r in rows)]
        short = [label for label in finished if len(seasons[label]) != 380]
        return [f"{path.name}: season {label} has {len(seasons[label])} matches, expected 380" for label in short]
    fixtures = data.get("fixtures") if isinstance(data, dict) else None
    if not isinstance(fixtures, list):
        return [f"{path.name}: has no 'fixtures' list"]
    bad = [f.get("id") for f in fixtures if abs(sum((f.get("probs") or {}).values()) - 1) > 0.01]
    return [f"{path.name}: {len(bad)} fixture(s) whose probabilities do not add up to 1 (first: {bad[0]})"] if bad else []


class Command(BaseCommand):
    help = "Check the generated data files are valid (no conflict markers, enough clubs, complete seasons, probabilities sum to 1). Exit code 1 if not."

    def add_arguments(self, parser):
        parser.add_argument("--teams", default=str(DATA / "pl_teams.json"))
        parser.add_argument("--history", default=str(DATA / "pl_history.json"))
        parser.add_argument("--predictions", default=str(DATA / "pl_predictions.json"))
        parser.add_argument("--archive", default=str(DATA / "openfootball.json"))

    def handle(self, *args, **options):
        problems = check_files(options["teams"], options["history"], predictions_path=options["predictions"],
                               archive_path=options["archive"])
        if problems:
            raise CommandError("The data files are not fit to commit:\n  " + "\n  ".join(problems))
        self.stdout.write(self.style.SUCCESS("Data files are valid."))
