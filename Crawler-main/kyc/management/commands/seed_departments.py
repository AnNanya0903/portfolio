from django.core.management.base import BaseCommand

from kyc.departments import seed_departments


class Command(BaseCommand):
    help = "Create the five departments and their contact emails. Safe to run more than once."

    def handle(self, *args, **options):
        result = seed_departments()
        self.stdout.write(self.style.SUCCESS(
            f"Departments added: {result['departments']}, emails added: {result['emails']} (existing ones were left alone)."))
