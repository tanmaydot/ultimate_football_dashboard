from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from ufd_app import history, pitchapi_source, pl_data


class Command(BaseCommand):
    help = (
        "Fetch Premier League team data from football-data.org, PitchAPI (formations, possession, player numbers), "
        "apifootball.com (venues) and footballdata.io and write the JSON file the dashboard reads."
    )

    def add_arguments(self, parser):
        parser.add_argument("--output", default=str(pl_data.DEFAULT_OUTPUT),
                            help="Where to write the JSON (default: ufd_app/data/pl_teams.json)")
        parser.add_argument("--skip-events", action="store_true",
                            help="Skip the per-match apifootball calls (no formations / possession, fewer requests)")
        parser.add_argument("--skip-pitchapi", action="store_true",
                            help="Don't use PitchAPI: formations, possession and player numbers come from apifootball again")
        parser.add_argument("--pitchapi-harvest", default=None, metavar="FOLDER",
                            help="Read PitchAPI data from a harvest_pitchapi folder instead of calling the API (no key needed)")
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
        pa = None
        if not options["skip_pitchapi"]:
            if options["pitchapi_harvest"]:
                pa = pitchapi_source.HarvestSource(options["pitchapi_harvest"])
                if not pa.is_configured():
                    raise CommandError(
                        f"No PitchAPI harvest at {Path(options['pitchapi_harvest']).resolve()} (it needs a 'matches' folder inside). "
                        "The path is relative to where you run the command: from the repo root use ufd/pitchapi_raw.")
            else:
                pa = pitchapi_source.PitchApiSource()
            if not pa.is_configured():
                self.stdout.write(self.style.WARNING(
                    "PitchAPI is not configured (PITCHAPI_API_KEY is not set): using apifootball for formations and player numbers."))
        io = None if options["skip_footballdata_io"] else pl_data.FootballDataIoSource()

        store = None
        if not options["no_history"]:
            if options["reset_history"]:
                Path(options["history"]).unlink(missing_ok=True)
            store = history.History.load(options["history"])
        self.stdout.write("Building Premier League data. This takes a few minutes because of API rate limits.")
        try:
            dataset = pl_data.build_dataset(fd, ap, log=self.stdout.write, include_events=not options["skip_events"], io=io,
                                           history=store, pa=pa, io_max_age_hours=options["io_max_age_hours"])
        except pl_data.FetchError as exc:
            raise CommandError(str(exc))

        output = Path(options["output"])
        pl_data.write_json_atomic(output, dataset)
        if store is not None:
            store.save()
            self.stdout.write(f"History saved to {store.path}")
        teams = len(dataset["teams"])
        with_pa = sum(1 for t in dataset["teams"].values() if t["sources"].get("pitchapi"))
        pitch_note = f"{with_pa} from PitchAPI, " if with_pa else ""
        with_ap = sum(1 for t in dataset["teams"].values() if t["sources"]["apifootball"])
        with_io = sum(1 for t in dataset["teams"].values() if t["sources"]["footballdata_io"])
        self.stdout.write(self.style.SUCCESS(
            f"Wrote {teams} teams ({pitch_note}{with_ap} enriched from apifootball, {with_io} from footballdata.io) to {output}"))
        unmatched_pa = dataset["meta"]["report"].get("unmatched_pitchapi") or []
        if unmatched_pa:
            self.stdout.write(self.style.WARNING("No PitchAPI match for: " + ", ".join(unmatched_pa)))
        unmatched_players = dataset["meta"]["report"].get("unmatched_pitchapi_players") or []
        if unmatched_players:
            self.stdout.write(self.style.WARNING(
                f"{len(unmatched_players)} PitchAPI players who appeared are not in the squad lists: " + ", ".join(unmatched_players[:10])))
        unmatched = dataset["meta"]["report"]["unmatched_football_data"]
        if unmatched:
            self.stdout.write(self.style.WARNING("No apifootball match for: " + ", ".join(unmatched)))
        unmatched_io = dataset["meta"]["report"]["unmatched_footballdata_io"]
        if unmatched_io:
            self.stdout.write(self.style.WARNING("No footballdata.io match for: " + ", ".join(unmatched_io)))
