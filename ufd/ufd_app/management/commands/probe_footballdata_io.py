"""Capture real footballdata.io responses so the merge code can be written against facts.

Makes about a dozen requests (the free plan allows 2,000 a month) and writes a
trimmed copy of each response to a JSON file. The API key is never written.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from ufd_app.footballdata_io import FootballDataIoClient, FootballDataIoError

SAMPLE_ITEMS = 3  # keep this many items of any list; the shape matters, not the volume
ID_KEYS = ("id", "league_id", "team_id", "match_id", "slug", "uuid")


def trim(value, depth=0):
    """Shrink a response for saving: long lists are cut and the cut is recorded."""
    if isinstance(value, list):
        kept = [trim(v, depth + 1) for v in value[:SAMPLE_ITEMS]]
        if len(value) > SAMPLE_ITEMS:
            kept.append(f"... {len(value) - SAMPLE_ITEMS} more items not saved")
        return kept
    if isinstance(value, dict):
        return {k: trim(v, depth + 1) for k, v in value.items()}
    return value


def items_of(data):
    """The list of records inside ``data`` (a list, or a dict wrapping one)."""
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    if isinstance(data, dict):
        for value in data.values():
            if isinstance(value, list):
                return [d for d in value if isinstance(d, dict)]
    return []


def pick_id(item):
    """(key, value) of the first identifier-looking field, without assuming which one the API uses."""
    for key in ID_KEYS:
        if item.get(key) not in (None, ""):
            return key, item[key]
    return None, None


def find_by_text(items, needle):
    needle = needle.lower()
    for item in items:
        if any(isinstance(v, str) and needle in v.lower() for v in item.values()):
            return item
    return None


class Command(BaseCommand):
    help = "Save trimmed real footballdata.io responses (teams, players, stats, standings) to a JSON file."

    def add_arguments(self, parser):
        parser.add_argument("--output", default="footballdata_io_probe.json")
        parser.add_argument("--league-id", help="Skip the search and use this league id / slug")

    def handle(self, *args, **options):
        client = FootballDataIoClient()
        if not client.is_configured():
            raise CommandError("FOOTBALLDATA_IO_API_KEY is not set.")

        log = []

        def call(path, params=None):
            """One request. Returns the envelope, or None (recorded) if it failed."""
            try:
                body = client.get(path, params)
            except FootballDataIoError as exc:
                log.append({"path": path, "params": params, "ok": False, "error": str(exc)})
                self.stdout.write(self.style.WARNING(f"  failed  {path}: {exc}"))
                if exc.status in (401, 403, 429):
                    raise CommandError(str(exc))  # nothing else will work; stop spending requests
                return None
            log.append({"path": path, "params": params, "ok": True, "response": trim(body)})
            self.stdout.write(f"  ok      {path}")
            return body

        self.stdout.write("Probing footballdata.io ...")
        call("/meta/status")
        call("/account/usage")
        call("/meta/coverage")
        england = call("/countries/england/leagues")

        league_key = league_value = None
        if options["league_id"]:
            league_key, league_value = "given", options["league_id"]
        elif england:
            item = find_by_text(items_of(england.get("data")), "premier league")
            if item:
                league_key, league_value = pick_id(item)
        if league_value is None:
            self.stdout.write(self.style.WARNING(
                "Couldn't find the Premier League in /countries/england/leagues. "
                "Look in the saved file for its id and re-run with --league-id."))
        else:
            self.stdout.write(f"  Premier League identified by {league_key} = {league_value}")
            teams = call(f"/leagues/{league_value}/teams", {"limit": 100})
            call(f"/leagues/{league_value}/standings")
            matches = call(f"/leagues/{league_value}/matches", {"limit": 1})

            team = items_of(teams.get("data"))[0] if teams and items_of(teams.get("data")) else None
            team_key, team_id = pick_id(team) if team else (None, None)
            if team_id is not None:
                call(f"/teams/{team_id}")
                call(f"/teams/{team_id}/players", {"limit": 5})
                call(f"/teams/{team_id}/stats")
            match = items_of(matches.get("data"))[0] if matches and items_of(matches.get("data")) else None
            match_key, match_id = pick_id(match) if match else (None, None)
            if match_id is not None:
                call(f"/matches/{match_id}/stats")

        result = {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "premier_league_identified_by": {"field": league_key, "value": league_value},
            "requests": log,
        }
        output = Path(options["output"])
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        ok = sum(1 for entry in log if entry["ok"])
        self.stdout.write(self.style.SUCCESS(f"Saved {ok}/{len(log)} responses to {output}"))
        self.stdout.write("The file contains no API key. Send it over and the merge can be written against the real fields.")
