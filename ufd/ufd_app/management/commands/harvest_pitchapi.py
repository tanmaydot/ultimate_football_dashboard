import time
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from ufd_app import pitchapi, pitchapi_harvest

DEFAULT_OUT = Path(__file__).resolve().parents[3] / "pitchapi_raw"


class Command(BaseCommand):
    help = ("Download everything PitchAPI has for a league (default: Premier League) and keep every raw response as JSON. "
            "Resumable: run it again and it carries on where it stopped. Needs PITCHAPI_API_KEY.")

    def add_arguments(self, parser):
        parser.add_argument("--output", default=str(DEFAULT_OUT), help=f"Folder for the raw files (default: {DEFAULT_OUT})")
        parser.add_argument("--league", default="Premier League", help="League name to look for in /v1/leagues")
        parser.add_argument("--league-id", help="Skip the name lookup and use this id (l_xxxxxx)")
        parser.add_argument("--seasons", help="Comma-separated seasons, e.g. 2025/2026,2024/2025 (default: the current season)")
        parser.add_argument("--all-seasons", action="store_true", help="Every season the API lists for the league (many thousands of requests)")
        parser.add_argument("--endpoints", help="Only these match endpoints, e.g. lineups,stats,events,players")
        parser.add_argument("--include-upcoming", action="store_true", help="Also store fixtures, with predicted lineups and head-to-head")
        parser.add_argument("--per-player", action="store_true", help="Also fetch the per-player match endpoints (4 calls per player per match)")
        parser.add_argument("--no-profiles", action="store_true", help="Skip /v1/teams/{id} and /v1/players/{id}")
        parser.add_argument("--refresh", action="store_true", help="Download again even if a file already exists")
        parser.add_argument("--max-requests", type=int, help="Stop cleanly after this many requests")
        parser.add_argument("--max-minutes", type=float, help="Stop cleanly after this many minutes")
        parser.add_argument("--pause", type=float, default=pitchapi.MIN_INTERVAL, help="Seconds between calls (default %(default)s; it slows itself on 429)")
        parser.add_argument("--zip", help="Also pack the result into this .zip file when done")
        parser.add_argument("--base-url", help="Override the API address (for tests)")

    def handle(self, *args, **options):
        client = pitchapi.PitchApiClient(base_url=options["base_url"], min_interval=options["pause"])
        if not client.is_configured():
            raise CommandError("PITCHAPI_API_KEY is not set.")
        deadline = time.time() + options["max_minutes"] * 60 if options["max_minutes"] else None
        harvester = pitchapi_harvest.Harvester(client, options["output"], log=self.stdout.write, refresh=options["refresh"],
                                              max_requests=options["max_requests"], deadline=deadline)
        self.stdout.write(f"Harvesting PitchAPI into {options['output']} (safe to stop and re-run)")
        try:
            index = harvester.run(
                league_name=options["league"], league_id=options["league_id"],
                seasons=[s.strip() for s in options["seasons"].split(",")] if options["seasons"] else None,
                all_seasons=options["all_seasons"],
                endpoints=[e.strip() for e in options["endpoints"].split(",")] if options["endpoints"] else None,
                include_upcoming=options["include_upcoming"], per_player=options["per_player"], profiles=not options["no_profiles"])
        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING("Stopped by you. Everything fetched so far is kept; run the same command to continue."))
            index = harvester._finish(time.time())

        if harvester.auth_failed:
            raise CommandError("PitchAPI rejected the key (401). Check PITCHAPI_API_KEY; nothing was fetched.")
        self.stdout.write("")
        for kind, counts in index["by_endpoint"].items():
            self.stdout.write(f"  {kind:<22} new {counts['ok']:>5}  already had {counts['cached']:>5}  "
                              f"not available {counts['unavailable']:>4}  failed {counts['failed']:>3}")
        self.stdout.write(f"{index['files_on_disk']} files on disk, {index['requests_this_run']} requests this run, "
                          f"{index['rate_limited_429']} rate-limit pauses.")
        if index["stopped_early"]:
            self.stdout.write(self.style.WARNING(f"Stopped early: {index['stopped_early']}. Run again to continue."))
        if options["zip"]:
            path = pitchapi_harvest.zip_folder(options["output"], options["zip"])
            self.stdout.write(self.style.SUCCESS(f"Packed into {path} ({Path(path).stat().st_size / 1e6:.1f} MB)"))
        if index["failures"]:
            raise CommandError(f"{len(index['failures'])} request(s) failed; see {options['output']}/_index.json. Re-run to retry them.")
