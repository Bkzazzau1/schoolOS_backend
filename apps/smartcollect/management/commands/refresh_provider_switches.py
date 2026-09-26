from django.core.management.base import BaseCommand

from apps.schools.models import School
from apps.smartcollect import switching
from apps.smartcollect.constants import OPEN_SWITCH
from apps.smartcollect.models import ProviderSwitch


class Command(BaseCommand):
    help = (
        "Look again at every planned provider switch: one whose date has come and that nothing stands in the way of becomes READY TO SWITCH. "
        "It never applies a switch: only a person does that."
    )

    def handle(self, *args, **options):
        ready = 0
        for school in School.objects.filter(pk__in=ProviderSwitch.objects.filter(status__in=OPEN_SWITCH).values("school_id")):
            switch = switching.refresh(school)
            ready += bool(switch and switch.status == "ready_to_switch")
        self.stdout.write(f"{ready} provider switch(es) ready to be applied.")
