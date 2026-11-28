"""Build each compound's standard current graph from its normal detected batches.

    python manage.py build_standard_curves --all
    python manage.py build_standard_curves --compound AG-7020 [--force]
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from compounding import iot_standards


class Command(BaseCommand):
    help = "Build the next standard version per compound (needs IOT_STANDARD_MIN_BATCHES normal batches)."

    def add_arguments(self, parser):
        parser.add_argument("--all", action="store_true")
        parser.add_argument("--compound", default="")
        parser.add_argument("--force", action="store_true", help="Build even when the batches are unchanged")

    def handle(self, *args, **options):
        if options["all"]:
            for item in iot_standards.rebuild_all():
                status = f"v{item['version']}" if item.get("version") else "standard not ready"
                self.stdout.write(f"{item['compound']}: {item['batches']} batches, {status}")
            return
        compound = options["compound"].strip()
        if not compound:
            raise CommandError("Pass --all or --compound")
        try:
            standard = iot_standards.build_standard(compound, force=options["force"])
        except iot_standards.StandardBuildError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(
            f"{compound}: v{standard.version} from {standard.built_from_n} batches, {len(standard.points)} points."
        ))
