from django.core.management.base import BaseCommand, CommandError

from ufd_app import club_extras_refresh as cer
from ufd_app import dashboard, pl_data


class Command(BaseCommand):
    help = ("Refresh data/club_extras.auto.json (stadium + honours) from the Transfermarkt API and Wikidata. "
            "Never touches the hand-kept club_extras.json. A club that fails keeps its previous entry.")

    def add_arguments(self, parser):
        parser.add_argument("--only", nargs="*", help="football-data team ids to refresh (default: every club in pl_teams.json)")
        parser.add_argument("--dry-run", action="store_true", help="Fetch and print the summary but do not write the file")
        parser.add_argument("--pause", type=float, default=1.0, help="Seconds between HTTP calls (default 1)")

    def handle(self, *args, **options):
        data = dashboard.load_pl_data()
        if data is None:
            raise CommandError(f"No team data found at {pl_data.DEFAULT_OUTPUT}. Run build_pl_data first.")
        team_ids = [t for t in data["teams"] if not options["only"] or t in options["only"]]
        if not team_ids:
            raise CommandError("No matching teams.")

        existing = cer.read_auto()
        result, summary = cer.refresh_all(cer.Fetcher(pause=options["pause"]), team_ids, existing)

        self.stdout.write(f"Refreshed {len(summary['ok'])}/{len(team_ids)} clubs.")
        for tid, why in summary["failed"].items():
            self.stdout.write(self.style.WARNING(f"  FAILED {tid}: {why} (kept previous entry)"))
        for tid in summary["skipped_no_ids"]:
            self.stdout.write(self.style.WARNING(f"  {tid}: not in CLUB_IDS (club_extras_refresh.py) - skipped"))
        for tid, notes in summary["review"].items():
            for note in notes:
                self.stdout.write(f"  review {tid}: {note}")
        for tid, titles in summary["unmapped"].items():
            self.stdout.write(f"  {tid}: unmapped Transfermarkt titles: {', '.join(titles)}")

        if options["dry_run"]:
            self.stdout.write("Dry run: nothing written.")
        else:
            cer.write_auto(result)
            self.stdout.write(self.style.SUCCESS(f"Wrote {cer.AUTO_PATH}"))
        if summary["failed"] and not summary["ok"]:
            raise CommandError("Every club failed - is the Transfermarkt API reachable?")