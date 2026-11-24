"""Expire old KYC links and send the reminder emails. App Engine cron runs the same through
/kyc/api/cron/kyc-links/ (cron.yaml); this command is for local runs and other schedulers."""
from django.core.management.base import BaseCommand

from kyc import link_requests


class Command(BaseCommand):
    help = "Expire KYC links past their time and send the 48-hour reminders (KYC_LINK_DRY_RUN decides sending)."

    def handle(self, *args, **options):
        self.stdout.write(str(link_requests.run_jobs()))
