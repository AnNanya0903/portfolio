"""Build a process group's next standard curve from its lab-passed batches.

    python manage.py build_standard_curve "Group A"
    python manage.py build_standard_curve 3
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from compounding import iot_curves
from compounding.models import ProcessGroup


class Command(BaseCommand):
    help = "Build the next standard curve version for a process group (needs IOT_CURVE_MIN_BATCHES passes)."

    def add_arguments(self, parser):
        parser.add_argument("group", help="Process group name or id")

    def handle(self, *args, **options):
        ref = options["group"].strip()
        group = ProcessGroup.objects.filter(name=ref).first()
        if group is None and ref.isdigit():
            group = ProcessGroup.objects.filter(pk=int(ref)).first()
        if group is None:
            raise CommandError(f"No process group called {ref!r}")
        try:
            curve = iot_curves.build_standard_curve(group)
        except iot_curves.CurveBuildError as exc:
            raise CommandError(str(exc)) from exc
        energy = (
            f"{curve.energy_mean:.2f} ± {curve.energy_std:.2f} kWh"
            if curve.energy_mean is not None and curve.energy_std is not None
            else "n/a"
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"{group.name}: curve v{curve.version} from {curve.built_from_n} batches, "
                f"{len(curve.points)} points every {curve.step_s} s, energy {energy}."
            )
        )
