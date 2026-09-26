import itertools
import json

from cryptography.fernet import Fernet
from django.test import override_settings
from django.utils import timezone

from apps.staff.tests.helpers import StaffTestCase
from apps.students.models import GuardianLink, Student, StudentEnrollment
from apps.sync.models import SyncRecord

from .. import ingestion, reconciliation
from ..models import BankAuditEvent, BankConnection
from ..providers.base import NormalizedTransaction
from ..vault import context_for, get_vault

_numbers = itertools.count(1)

KEY = Fernet.generate_key().decode()

#: Values that must never come back from the API, appear in the audit trail, or be logged.
SANDBOX_KEY = "sandbox-UNIQUE-KEY-777"
ACCOUNT = "0123456789"


@override_settings(BANKCONNECT_SECRET_KEYS=[KEY], BANKCONNECT_ENABLE_SANDBOX=True)
class BankTestCase(StaffTestCase):
    """A school with one person in every role, a second separate school, a working vault and the
    sandbox switched on."""

    def give_duty(self, member, duty="finance.collection_provider_manage", status="active"):
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

    def connect(self, who=None, label="School sandbox", key=SANDBOX_KEY, school=None, provider="sandbox", environment="test", credentials=None):
        """Connect a provider with the SCHOOL'S OWN credentials. (The sandbox stands in for Paystack / Monnify / Remita.)"""
        return self.api_post(
            "connections/",
            {"provider": provider, "environment": environment, "label": label, "credentials": credentials or {"sandbox_key": key}},
            who=who, school=school,
        )

    def connected(self, who=None, school=None, **kw):
        """A connected provider. Returns (its JSON, the webhook path an authorised person is shown)."""
        created = self.connect(who=who, school=school, **kw)
        self.assertEqual(created.status_code, 201, created.json())
        connection = created.json()["connection"]
        setup = self.api_get(f"connections/{connection['id']}/webhook/", who=who, school=school)
        return connection, setup.json()["webhook"]["path"] if setup.status_code == 200 else None

    def legacy_connection(self, purpose="tuition", school=None, mask="****1111", bank="GTBank", sandbox=False):
        """A connection made by the earlier bank-account model (no provider of Smart Money Collection). The fuzzy student-matching engine and
        the review queue still serve such payments, and these tests exercise that engine; a Smart Money Collection payment is matched by the
        account it was paid into, never by guessing."""
        return BankConnection.objects.create(
            school=school or self.school, provider="legacy_bank", connection_type="direct_bank_api", bank_name=bank, account_name="SCHOOL",
            account_mask=mask, purpose=purpose, label=f"{purpose} account", status="connected", is_sandbox=sandbox,
        )

    def row(self, connection_json) -> BankConnection:
        return BankConnection.objects.get(id=connection_json["id"])

    def reseal(self, connection: BankConnection, secret: dict):
        """Put a different credential in the vault, as if the provider had changed something."""
        connection.sealed_credentials = get_vault().seal(context_for(connection), secret)
        connection.save(update_fields=["sealed_credentials"])

    def secret_of(self, connection: BankConnection) -> dict:
        return get_vault().open(context_for(connection), connection.sealed_credentials)

    # -- what must never leak ----------------------------------------------------------------

    def assert_no_secrets(self, *things):
        secrets_ = [SANDBOX_KEY]
        for connection in BankConnection.objects.exclude(sealed_credentials=b""):
            opened = self.secret_of(connection)
            secrets_ += [opened.get("webhook_secret", "not-a-secret"), opened.get("webhook_token", "not-a-secret")]
        text = " ".join(t if isinstance(t, str) else json.dumps(t, default=str) for t in things)
        for secret in secrets_:
            self.assertNotIn(secret, text)

    def audit_kinds(self, connection_json=None):
        events = BankAuditEvent.objects.all()
        if connection_json:
            events = events.filter(connection_id=connection_json["id"])
        return [e.kind for e in events.order_by("at", "id")]

    # -- students and payments ---------------------------------------------------------------

    def make_student(self, code, first, surname, *, guardian=None, phone="", admission=None, school=None,
                     status="active", class_name=""):
        school = school or self.school
        student = Student.objects.create(
            school=school, student_code=code, admission_number=admission or f"ADM/{next(_numbers):04d}",
            first_name=first, surname=surname, status=status,
        )
        if guardian:
            GuardianLink.objects.create(student=student, name=guardian, phone=phone, is_primary=True)
        if class_name:
            StudentEnrollment.objects.create(
                school=school, student=student, academic_section="Primary", class_name=class_name, started_at=timezone.now()
            )
        return student

    def add_guardian(self, student, name, phone=""):
        return GuardianLink.objects.create(student=student, name=name, phone=phone)

    def deposit(self, connection, *, reconcile=True, **over):
        """A credit arriving on the connection, stored and (unless told not to) reconciled. Returns the row."""
        fields = dict(
            external_transaction_id=f"DEP-{next(_numbers)}", direction="credit", amount_minor=5_000_000,
            transaction_date=timezone.now(),
        )
        fields.update(over)
        result = ingestion.ingest(connection, NormalizedTransaction(**fields))
        assert result.transaction is not None, "the deposit was refused as malformed"
        if reconcile:
            reconciliation.reconcile_pending(connection.school)
        result.transaction.refresh_from_db()
        return result.transaction
