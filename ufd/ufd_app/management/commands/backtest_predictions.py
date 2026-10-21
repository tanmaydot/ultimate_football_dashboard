import json
from datetime import date

from django.core.management.base import BaseCommand, CommandError

from ufd_app import history as history_store
from ufd_app import openfootball, pl_data, predictions, validation
from ufd_app.util import read_json, write_json_atomic


class Command(BaseCommand):
    help = ("Score the match and player models on history (walk-forward, no leakage) and write data/pl_validation.json. "
            "Takes about a minute; --tune also re-selects the match parameters on 2005-06 to 2015-16 (a few minutes).")

    def add_arguments(self, parser):
        parser.add_argument("--output", default=str(predictions.DEFAULT_VALIDATION))
        parser.add_argument("--archive", default=str(openfootball.DEFAULT_ARCHIVE))
        parser.add_argument("--pl-data", default=str(pl_data.DEFAULT_OUTPUT))
        parser.add_argument("--history", default=str(history_store.DEFAULT_HISTORY))
        parser.add_argument("--tune", action="store_true", help="Search the match-model parameters again before testing")

    def handle(self, *args, **options):
        archive = openfootball.Archive.load(options["archive"])
        if not archive.seasons:
            raise CommandError("No OpenFootball data: run build_predictions first.")
        try:
            pl, history = read_json(options["pl_data"]), read_json(options["history"])
        except (OSError, ValueError) as exc:
            raise CommandError(f"Run build_pl_data first ({exc}).")
        report = validation.run(pl, history, archive, today=date.today(), tune=options["tune"], log=self.stdout.write)
        write_json_atomic(options["output"], report)
        team = report["team_model"]
        self.stdout.write(self.style.SUCCESS(
            f"Match model on {team['matches']} matches ({team['test_seasons'][0]} to {team['test_seasons'][-1]}): "
            f"accuracy {team['scores']['model']['accuracy']:.1%} (constant forecast "
            f"{team['scores']['constant_home_draw_away_rates']['accuracy']:.1%}), RPS {team['scores']['model']['rps']} "
            f"(constant {team['scores']['constant_home_draw_away_rates']['rps']})."))
        self.stdout.write(json.dumps(team["rps_difference_vs"]["constant_home_draw_away_rates"]))
        self.stdout.write(f"Written to {options['output']}")
