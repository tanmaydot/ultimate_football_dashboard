from django.core.management.base import BaseCommand, CommandError

from ufd_app import pitchapi_audit


class Command(BaseCommand):
    help = "Check that a harvest_pitchapi folder agrees with itself (scores, formations, possession, cards)."

    def add_arguments(self, parser):
        parser.add_argument("folder", help="The harvest folder, e.g. ufd/pitchapi_raw")

    def handle(self, *args, **options):
        report = pitchapi_audit.audit(options["folder"])
        if not report["matches"]:
            raise CommandError("No finished matches found in that folder.")
        self.stdout.write(f"{report['matches']} finished matches")
        for check in report["checks"]:
            line = f"  [{'ok' if check['ok'] else 'CHECK'}] {check['name']}: {check['detail']}"
            self.stdout.write(self.style.SUCCESS(line) if check["ok"] else self.style.WARNING(line))
