import json

from django.core.management.base import BaseCommand

from enquiries import scheduler


class Command(BaseCommand):
    help = ("Run the scheduled automations once: automatic follow-ups, customer emails and reply checks. "
            "The same thing App Engine's hourly cron does. Whether anything is really sent still follows "
            "FOLLOW_UP_AUTO_ENABLED and FOLLOW_UP_AUTO_DRY_RUN.")

    def handle(self, *args, **options):
        result = scheduler.run_all(trigger="command")
        self.stdout.write(json.dumps(result, indent=2, default=str))
