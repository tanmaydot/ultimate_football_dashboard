from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from ufd_app import history, pl_data


class Command(BaseCommand):
    help = (
        "Fetch Premier League team data from football-data.org, apifootball.com and footballdata.io "
        "and write the JSON file the dashboard reads."
    )

    def add_arguments(self, parser):
        parser.add_argument("--output", default=str(pl_data.DEFAULT_OUTPUT),
                            help="Where to write the JSON (default: ufd_app/data/pl_teams.json)")
        parser.add_argument("--skip-events", action="store_true",
                            help="Skip the per-match apifootball calls (no formations / possession, fewer requests)")
        parser.add_argument("--skip-footballdata-io", action="store_true",
                            help="Don't call footballdata.io (about 23 of its 2,000 monthly requests)")
        parser.add_argument("--history", default=str(history.DEFAULT_HISTORY),
                            help="Persistent season history (default: ufd_app/data/pl_history.json)")
        parser.add_argument("--no-history", action="store_true", help="Don't read or write the history file")
        parser.add_argument("--reset-history", action="store_true", help="Delete the stored history first and start again")
        parser.add_argument("--io-max-age-hours", type=float, default=24,
                            help="Reuse footballdata.io team stats fetched within this many hours (default 24)")
        parser.add_argument("--fd-interval", type=float, default=pl_data.FD_MIN_INTERVAL,
                            help="Seconds between football-data calls (free plan: 6.5 keeps you under 10 a minute)")

    def handle(self, *args, **options):
        fd = pl_data.FootballDataClient(min_interval=options["fd_interval"])
        if not fd.is_configured():
            raise CommandError("FOOTBALL_DATA_API_KEY is not set.")
        ap = pl_data.ApiFootballSource()
        io = None if options["skip_footballdata_io"] else pl_data.FootballDataIoSource()

        store = None
        if not options["no_history"]:
            if options["reset_history"]:
                Path(options["history"]).unlink(missing_ok=True)
            store = history.History.load(options["history"])
        self.stdout.write("Building Premier League data. This takes a few minutes because of API rate limits.")
        try:
            dataset = pl_data.build_dataset(fd, ap, log=self.stdout.write, include_events=not options["skip_events"], io=io,
                                           history=store, io_max_age_hours=options["io_max_age_hours"])
        except pl_data.FetchError as exc:
            raise CommandError(str(exc))

        output = Path(options["output"])
        pl_data.write_json_atomic(output, dataset)
        if store is not None:
            store.save()
            self.stdout.write(f"History saved to {store.path}")
        teams = len(dataset["teams"])
        with_ap = sum(1 for t in dataset["teams"].values() if t["sources"]["apifootball"])
        with_io = sum(1 for t in dataset["teams"].values() if t["sources"]["footballdata_io"])
        self.stdout.write(self.style.SUCCESS(
            f"Wrote {teams} teams ({with_ap} enriched from apifootball, {with_io} from footballdata.io) to {output}"))
        unmatched = dataset["meta"]["report"]["unmatched_football_data"]
        if unmatched:
            self.stdout.write(self.style.WARNING("No apifootball match for: " + ", ".join(unmatched)))
        unmatched_io = dataset["meta"]["report"]["unmatched_footballdata_io"]
        if unmatched_io:
            self.stdout.write(self.style.WARNING("No footballdata.io match for: " + ", ".join(unmatched_io)))
