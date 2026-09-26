from apps.staff.tests.helpers import StaffTestCase
from apps.sync.models import SyncRecord

from ..constants import BILLING_AUTHORITY_DUTY


class ReceivablesTestCase(StaffTestCase):
    """A school with one person in every role, and a second, separate school."""

    def give_duty(self, member, duty=BILLING_AUTHORITY_DUTY, status="active", school=None):
        SyncRecord.objects.update_or_create(
            school=school or self.school, entity_type="owner_job_assignment", entity_id=f"JOB-{member.id}-{duty}",
            defaults={"payload": {"registeredStaffId": f"S-{member.id}", "duties": [duty], "status": status,
                                   "membershipId": str(member.id)}},
        )
