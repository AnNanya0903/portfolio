"""Build standard current graphs from labelled, normal detected batches.

    python manage.py build_standard_curves --all              # every compound/group with enough batches
    python manage.py build_standard_curves --compound AG-7020
    python manage.py build_standard_curves --group "NBR 15 min"
    python manage.py build_standard_curves --compound AG-7020 --force   # new version even if unchanged
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from compounding import iot_standards
from compounding.models import ProcessGroup


class Command(BaseCommand):
    help = "Build the next version of standard current curves (needs IOT_STANDARD_MIN_BATCHES batches)."

    def add_arguments(self, parser):
        parser.add_argument("--all", action="store_true")
        parser.add_argument("--compound", default="")
        parser.add_argument("--group", default="", help="Process group name")
        parser.add_argument("--force", action="store_true", help="Build even when the batches are unchanged")

    def handle(self, *args, **options):
        if options["all"]:
            for item in iot_standards.rebuild_all():
                status = f"v{item['version']}" if item.get("version") else "not enough batches"
                self.stdout.write(f"{item['label']}: {item['batches']} batches, {status}")
            return
        if options["group"]:
            group = ProcessGroup.objects.filter(name=options["group"]).first()
            if group is None:
                raise CommandError(f"No process group called {options['group']!r}")
            key = f"group:{group.pk}"
        elif options["compound"]:
            key, _label, _group = iot_standards.key_for(options["compound"].strip())
        else:
            raise CommandError("Pass --all, --compound or --group")
        try:
            standard = iot_standards.build_standard(key, force=options["force"])
        except iot_standards.StandardBuildError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(
            f"{standard.label}: v{standard.version} from {standard.built_from_n} batches, {len(standard.points)} points."
        ))
