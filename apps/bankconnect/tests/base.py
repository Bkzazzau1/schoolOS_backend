import json

from cryptography.fernet import Fernet
from django.test import override_settings

from apps.staff.tests.helpers import StaffTestCase
from apps.sync.models import SyncRecord

from ..models import BankAuditEvent, BankConnection
from ..vault import context_for, get_vault

KEY = Fernet.generate_key().decode()

#: Values that must never come back from the API, appear in the audit trail, or be logged.
SANDBOX_KEY = "sandbox-UNIQUE-KEY-777"
ACCOUNT = "0123456789"


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

    # -- calling the API ---------------------------------------------------------------------

    def path(self, tail, school=None):
        return f"/api/v1/schools/{(school or self.school).id}/collections/{tail}"

    def api_get(self, tail, who=None, school=None):
        self.client.force_authenticate((who or self.owner).user)
        return self.client.get(self.path(tail, school))

    def api_post(self, tail, body=None, who=None, school=None):
        self.client.force_authenticate((who or self.owner).user)
        return self.client.post(self.path(tail, school), body or {}, format="json")

    # -- making connections ------------------------------------------------------------------

    def connect(self, who=None, purpose="tuition", label="Tuition Collection", account=ACCOUNT, key=SANDBOX_KEY,
                school=None):
        return self.api_post(
            "connections/",
            {"provider": "sandbox", "purpose": purpose, "label": label,
             "credentials": {"sandbox_key": key, "account_number": account}},
            who=who, school=school,
        )

    def connected(self, who=None, school=None, **kw):
        """A confirmed connection. Returns (its JSON, the webhook path shown once)."""
        created = self.connect(who=who, school=school, **kw)
        self.assertEqual(created.status_code, 201, created.json())
        confirmed = self.api_post(f"connections/{created.json()['connection']['id']}/confirm/", who=who, school=school)
        self.assertEqual(confirmed.status_code, 200, confirmed.json())
        body = confirmed.json()
        return body["connection"], body.get("webhook", {}).get("path")

    def row(self, connection_json) -> BankConnection:
        return BankConnection.objects.get(id=connection_json["id"])

    def reseal(self, connection: BankConnection, secret: dict):
        """Put a different credential in the vault, as if the bank had changed something."""
        connection.sealed_credentials = get_vault().seal(context_for(connection), secret)
        connection.save(update_fields=["sealed_credentials"])

    def secret_of(self, connection: BankConnection) -> dict:
        return get_vault().open(context_for(connection), connection.sealed_credentials)

    # -- what must never leak ----------------------------------------------------------------

    def assert_no_secrets(self, *things):
        secrets_ = [SANDBOX_KEY, ACCOUNT]
        for connection in BankConnection.objects.exclude(sealed_credentials=b""):
            secrets_.append(self.secret_of(connection).get("webhook_secret", "not-a-secret"))
        text = " ".join(t if isinstance(t, str) else json.dumps(t, default=str) for t in things)
        for secret in secrets_:
            self.assertNotIn(secret, text)

    def audit_kinds(self, connection_json=None):
        events = BankAuditEvent.objects.all()
        if connection_json:
            events = events.filter(connection_id=connection_json["id"])
        return [e.kind for e in events.order_by("at", "id")]
