import itertools
from datetime import date

from cryptography.fernet import Fernet
from django.test import override_settings

from apps.academics.models import AcademicTerm
from apps.bankconnect import provider_connections
from apps.bankconnect.models import CollectionProviderConnection
from apps.bankconnect.providers import sandbox
from apps.receivables import families, schedules
from apps.receivables.tests.base import ReceivablesTestCase
from apps.students.models import GuardianLink
from apps.sync.models import SyncRecord

from .. import batches, jobs, policy, running
from ..models import CollectionGenerationBatch, CollectionGenerationBatchItem

KEY = Fernet.generate_key().decode()
N = 100
_numbers = itertools.count(1)

PREPARE, APPROVE, POLICY, PROVIDER = (
    "finance.collection_prepare", "finance.collection_approve", "finance.collection_policy_manage", "finance.collection_provider_manage",
)


@override_settings(BANKCONNECT_SECRET_KEYS=[KEY], BANKCONNECT_ENABLE_SANDBOX=True, SMART_COLLECTION_INLINE_JOBS=False)
class CollectTestCase(ReceivablesTestCase):
    """A school with a working vault, the sandbox provider connected and ACTIVE, an active session with two terms, and a maker, a checker and
    an owner. Families are made with `make_family`; nothing reaches a real provider."""

    def setUp(self):
        super().setUp()
        self.addCleanup(sandbox.clear_faults)
        self.session, self.term1, self.primary, self.jss = self.make_year()
        self.term2 = AcademicTerm.objects.create(
            session=self.session, code="T2", name="Second Term", sequence=2, starts_on=date(2027, 1, 11), ends_on=date(2027, 4, 10), status="planned",
        )
        self.maker = self.members["accountant"]
        self.checker = self.members["principal"]
        self.other_checker = self.members["administrator"]
        self.give_duties(self.maker, PREPARE)
        self.give_duties(self.checker, APPROVE)
        self.give_duties(self.other_checker, APPROVE)
        self.connection = self.connect_provider()

    # -- authority ------------------------------------------------------------------------------

    def give_duties(self, member, *duties, status="active"):
        SyncRecord.objects.update_or_create(
            school=self.school, entity_type="owner_job_assignment", entity_id=f"JOB-{member.id}",
            defaults={"payload": {"registeredStaffId": f"S-{member.id}", "duties": list(duties), "status": status, "membershipId": str(member.id)}},
        )

    # -- the provider -----------------------------------------------------------------------------

    def connect_provider(self, *, school=None, owner=None, activate=True, provider="sandbox", credentials=None, environment="test"):
        school = school or self.school
        owner = owner or (self.owner if school == self.school else self.other_owner)
        connection = provider_connections.connect(
            owner, provider=provider, environment=environment, label="Test provider", credentials=credentials or {"sandbox_key": "sandbox-key-1"}
        )
        if activate:
            provider_connections.activate(owner, connection.id)
        connection.refresh_from_db()
        return connection

    # -- families ---------------------------------------------------------------------------------

    def make_family(self, name="Bello", *, owes=100_000, term=None, email="parent@example.com", students=1, due=None, school=None, klass=None):
        """A family with `students` children, each owing `owes` naira for `term` (default: the first term), and a payer with an email."""
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

    def pay(self, family, amount_naira, *, account=None):
        """The family pays into its account: the provider's signed event, taken through the real webhook path and reconciled."""
        from apps.bankconnect import sandbox_tools

        account = account or family.collection_accounts.exclude(status__in=("closed", "failed")).first()
        result = sandbox_tools.deliver(
            self.connection, amount_minor=amount_naira * N, receiving_account_reference=account.account_number, sender_name="Payer",
        )
        assert result.outcome == "processed", result
        return account

    def into_term_two(self):
        """Move the fee system's clock into the second term: what was owed for the first is now a previous balance."""
        self.set_today(date(2027, 1, 12))

    # -- batches ------------------------------------------------------------------------------------

    def new_batch(self, *, term=None, who=None, **kw):
        return batches.create_batch(who or self.maker, session=self.session, term=term or self.term1, **kw)

    def items(self, batch):
        return {i.family_id: i for i in CollectionGenerationBatchItem.objects.filter(batch=batch)}

    def item(self, batch, family):
        return CollectionGenerationBatchItem.objects.get(batch=batch, family=family)

    def refetch(self, batch) -> CollectionGenerationBatch:
        return CollectionGenerationBatch.objects.get(pk=batch.pk)

    def approved(self, **kw):
        """A batch prepared by the maker, submitted, and approved by the checker."""
        batch = self.new_batch(**kw)
        batches.submit(self.maker, batch.id, expected_hash=batch.snapshot_hash)
        batch = self.refetch(batch)
        batches.approve(self.checker, batch.id, expected_hash=batch.snapshot_hash)
        return self.refetch(batch)

    def run_batch(self, batch, *, who=None):
        """Start an approved batch and let the worker carry out everything queued."""
        running.start_processing(who or self.maker, batch.id)
        jobs.drain()
        return self.refetch(batch)

    def set_policy(self, **values):
        policy.update_school_policy(self.owner, values)

    def live_accounts(self, family):
        return list(family.collection_accounts.exclude(status__in=("closed", "failed")))

    def sandbox_accounts(self):
        from apps.bankconnect.models import SandboxProviderAccount

        return SandboxProviderAccount.objects.filter(connection=self.connection)

    def connection_row(self) -> CollectionProviderConnection:
        return CollectionProviderConnection.objects.get(pk=self.connection.pk)
