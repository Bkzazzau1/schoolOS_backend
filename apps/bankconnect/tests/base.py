from cryptography.fernet import Fernet
from django.test import override_settings

from apps.staff.tests.helpers import StaffTestCase
from apps.sync.models import SyncRecord

KEY = Fernet.generate_key().decode()


@override_settings(BANKCONNECT_SECRET_KEYS=[KEY], BANKCONNECT_ENABLE_SANDBOX=True)
class BankTestCase(StaffTestCase):
    """A school with one person in every role, a second separate school, a working vault and the
    sandbox switched on."""

    def give_duty(self, member, duty="finance.bank_connections", status="active"):
        SyncRecord.objects.update_or_create(
            school=self.school, entity_type="owner_job_assignment", entity_id=f"JOB-{member.id}",
            defaults={"payload": {"registeredStaffId": f"S-{member.id}", "duties": [duty], "status": status,
                                   "membershipId": str(member.id)}},
        )
