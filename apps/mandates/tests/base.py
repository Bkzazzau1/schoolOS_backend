import itertools
from datetime import date

from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.utils import timezone

from apps.academics.models import AcademicTerm
from apps.receivables import families, schedules
from apps.receivables.tests.base import ReceivablesTestCase
from apps.schools.models import Membership, Role
from apps.students.models import GuardianLink
from apps.sync.models import SyncRecord

from .. import connections, consent, debit_batches, execution, jobs, mandate_provider, mandates
from ..constants import MandateStatus
from ..models import DirectDebitMandate, MandateDebitBatch, MandateDebitInstruction, SandboxMandate
from ..providers import sandbox

KEY = Fernet.generate_key().decode()
N = 100
_numbers = itertools.count(1)

MANAGE, PREPARE, APPROVE, PROVIDER = (
    "finance.mandate_manage", "finance.mandate_prepare", "finance.mandate_approve", "finance.mandate_provider_manage",
)
ACCOUNT = "0123456789"


@override_settings(BANKCONNECT_SECRET_KEYS=[KEY], MANDATES_ENABLE_SANDBOX=True, MANDATES_INLINE_JOBS=False)
class MandateTestCase(ReceivablesTestCase):
    """A school with a working vault, the sandbox mandate provider connected, an active session with two terms, and a manager, a maker, a checker
    and an owner. Families are made with `make_family`; nothing reaches a real provider."""

    def setUp(self):
        super().setUp()
        self.addCleanup(sandbox.clear_faults)
        self.session, self.term1, self.primary, self.jss = self.make_year()
        self.term2 = AcademicTerm.objects.create(
            session=self.session, code="T2", name="Second Term", sequence=2, starts_on=date(2027, 1, 11), ends_on=date(2027, 4, 10), status="planned",
        )
        self.manager = self.members["accountant"]
        self.maker = self.members["administrator"]
        self.checker = self.members["principal"]
        self.other_checker = self.members["staff"]
        self.give_duties(self.manager, MANAGE)
        self.give_duties(self.maker, PREPARE)
        self.give_duties(self.checker, APPROVE)
        self.give_duties(self.other_checker, APPROVE)
        self.connection = self.connect_provider()

    # -- authority ------------------------------------------------------------------------------

    def give_duties(self, member, *duties, status="active"):
        SyncRecord.objects.update_or_create(
            school=member.school, entity_type="owner_job_assignment", entity_id=f"JOB-{member.id}",
            defaults={"payload": {"registeredStaffId": f"S-{member.id}", "duties": list(duties), "status": status, "membershipId": str(member.id)}},
        )

    # -- the provider -----------------------------------------------------------------------------

    def connect_provider(self, *, school=None, provider="sandbox", credentials=None, environment="test"):
        school = school or self.school
        owner = self.owner if school == self.school else self.other_owner
        return connections.connect(owner, provider=provider, environment=environment, label="Test provider", credentials=credentials or {"sandbox_key": "sandbox-key-1"})

    # -- families and payers ----------------------------------------------------------------------

    def make_family(self, name="Bello", *, owes=100_000, term=None, email="parent@example.com", students=1, due=None, school=None, klass=None):
        """A family with `students` children, each owing `owes` naira for `term` (default: the first term), and a payer with an email and a phone."""
        school = school or self.school
        term = term or self.term1
        klass = klass or self.primary
        kids = []
        for i in range(students):
            student = self.make_student(f"{name}{i}", name, school=school, guardian=f"{name} Parent", phone=f"0803{next(_numbers):07d}")
            self.enroll(student, klass, self.session)
            kids.append(student)
        family = families.create_family(school, display_name=f"{name} family", actor=self.owner, students=kids)
        for kid in kids:
            families.link_guardians_of(family, kid, actor=self.owner)
        if email is not None:
            GuardianLink.objects.filter(student__in=kids).update(email=email)
        if owes:
            self.charge_family(family, kids, owes, term=term, due=due)
        return family

    def charge_family(self, family, kids, owes, *, term, due=None):
        due = due or (date(2026, 11, 20) if term == self.term1 else date(2027, 1, 25))
        schedule = schedules.create_schedule(family.school, session=self.session, term=term, name=f"{term.name} {family.code} {next(_numbers)}", actor=self.owner)
        for i, kid in enumerate(kids):
            schedules.add_item(
                schedule, actor=self.owner, code=f"TUI-{term.code}-{family.code}-{i}", name="Tuition", category="tuition", amount_minor=owes * N,
                due_date=due, scope="student", student=kid,
            )
        schedules.publish(schedule, actor=self.owner)
        return schedule

    def payer_of(self, family):
        return family.guardians.filter(is_active=True).first()

    def make_parent(self, family):
        """Give the family's payer a signed-in parent account, and return the parent's membership."""
        payer = self.payer_of(family)
        user = get_user_model().objects.create_user(f"parent{next(_numbers)}@home.ng", "Password-12345")
        membership = Membership.objects.create(user=user, school=family.school, role=Role.PARENT)
        GuardianLink.objects.filter(id=payer.guardian_id).update(account_user=user)
        return membership

    # -- mandates ---------------------------------------------------------------------------------

    def start_mandate(self, family, *, who=None, route="provider", maximum=500_000, connection=None, account=ACCOUNT, bank="058", **kw):
        payer = self.payer_of(family)
        return mandates.start(
            who or self.manager, family_id=family.id, payer_id=payer.id, connection_id=(connection or self.connection).id, bank_code=bank,
            account_number=account, maximum_amount_minor=maximum * N, consent_route=route, **kw,
        )

    def provider_row(self, mandate) -> SandboxMandate:
        return SandboxMandate.objects.get(connection=mandate.provider_connection, provider_ref=mandate.provider_mandate_reference)

    def activate_at_provider(self, mandate, *, status="active"):
        """The payer activates it with their bank, and the provider now says so."""
        SandboxMandate.objects.filter(provider_ref=mandate.provider_mandate_reference).update(status=status, debit_ready_at=timezone.now())
        return mandate_provider.refresh_mandate(mandate.id)

    def active_mandate(self, family, **kw):
        """A debit-ready mandate on the family, authorised with the provider."""
        mandate = self.start_mandate(family, **kw)
        return self.activate_at_provider(mandate)

    def reload(self, obj):
        return type(obj).objects.get(pk=obj.pk)

    # -- batches ----------------------------------------------------------------------------------

    def new_batch(self, *, term=None, who=None, **kw):
        return debit_batches.create_batch(who or self.maker, session=self.session, term=term or self.term1, **kw)

    def item(self, batch, family) -> MandateDebitInstruction:
        return MandateDebitInstruction.objects.get(batch=batch, family=family)

    def refetch(self, batch) -> MandateDebitBatch:
        return MandateDebitBatch.objects.get(pk=batch.pk)

    def select_all(self, batch):
        debit_batches.set_selection(self.maker, batch.id, select_all_eligible=True)
        return self.refetch(batch)

    def approved(self, **kw):
        """A batch prepared by the maker with every eligible family selected, submitted, and approved by the checker."""
        batch = self.select_all(self.new_batch(**kw))
        debit_batches.submit(self.maker, batch.id, expected_hash=batch.snapshot_hash)
        batch = self.refetch(batch)
        debit_batches.approve(self.checker, batch.id, expected_hash=batch.snapshot_hash)
        return self.refetch(batch)

    def run_batch(self, batch, *, who=None):
        """Start an approved batch and let the worker carry out everything queued."""
        execution.start(who or self.maker, batch.id)
        jobs.drain()
        return self.refetch(batch)

    # -- money ------------------------------------------------------------------------------------

    def owed(self, family) -> int:
        """What the ledger says the family owes, in kobo."""
        from apps.receivables import ledger

        return ledger.family_position(family).outstanding

    def pay_manually(self, family, naira):
        """The family pays the school some other way (a transfer, cash): a payment known to belong to it, settled by the ordinary pipeline."""
        from apps.bankconnect.models import BankTransaction
        from apps.receivables import payments

        tx = self.payment(naira * N)
        BankTransaction.objects.filter(pk=tx.pk).update(family=family, reconciliation_status="matched")
        payments.settle(BankTransaction.objects.get(pk=tx.pk))
        return tx

    def assert_no_secrets(self, *things):
        text = " ".join(str(t) for t in things)
        for secret in (ACCOUNT, "sandbox-key-1"):
            self.assertNotIn(secret, text)


def sealed_of(mandate: DirectDebitMandate) -> bytes:
    return bytes(DirectDebitMandate.objects.get(pk=mandate.pk).sealed_account_details)


__all__ = ["ACCOUNT", "APPROVE", "MANAGE", "MandateStatus", "MandateTestCase", "N", "PREPARE", "PROVIDER", "consent"]
