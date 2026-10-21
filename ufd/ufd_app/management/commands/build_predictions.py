from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from ufd_app import history as history_store
from ufd_app import openfootball, pl_data, predictions
from ufd_app.util import read_json, write_json_atomic


class Command(BaseCommand):
    help = ("Build the match and player predictions the dashboard shows (data/pl_predictions.json). Downloads the current "
            "OpenFootball season (every earlier season only once), then computes; no API key is needed.")

    def add_arguments(self, parser):
        parser.add_argument("--output", default=str(predictions.DEFAULT_PREDICTIONS))
        parser.add_argument("--archive", default=str(openfootball.DEFAULT_ARCHIVE), help="Cached OpenFootball seasons")
        parser.add_argument("--pl-data", default=str(pl_data.DEFAULT_OUTPUT))
        parser.add_argument("--history", default=str(history_store.DEFAULT_HISTORY))
        parser.add_argument("--validation", default=str(predictions.DEFAULT_VALIDATION),
                            help="Validation report to summarise inside the output (made by backtest_predictions)")
        parser.add_argument("--offline", action="store_true", help="Do not download anything; use the cached archive")
        parser.add_argument("--force-download", action="store_true", help="Download every season again")

    def handle(self, *args, **options):
        archive = openfootball.Archive.load(options["archive"])
        if not options["offline"]:
            fetched = archive.update(force=options["force_download"], log=lambda m: self.stdout.write(self.style.WARNING(m)))
            if fetched:
                archive.save(options["archive"])
            self.stdout.write(f"OpenFootball: {len(fetched)} season(s) downloaded ({len(archive.seasons)} cached).")
        if not archive.seasons:
            raise CommandError("No OpenFootball data: run without --offline once (needs internet).")
        try:
            pl, history = read_json(options["pl_data"]), read_json(options["history"])
        except (OSError, ValueError) as exc:
            raise CommandError(f"Run build_pl_data first ({exc}).")
        validation = None
        if Path(options["validation"]).exists():
            validation = read_json(options["validation"])
        document = predictions.build_predictions(pl, history, archive, validation=validation)
        write_json_atomic(options["output"], document)
        meta = document["meta"]
        self.stdout.write(self.style.SUCCESS(
            f"Wrote {len(document['fixtures'])} fixture forecasts and "
            f"{meta['report']['players']['players_predicted']} player outlooks to {options['output']}"))
        if meta["report"]["unmatched_fixtures"]:
            self.stdout.write(self.style.WARNING("Fixtures with an unknown club: " + ", ".join(meta["report"]["unmatched_fixtures"])))
