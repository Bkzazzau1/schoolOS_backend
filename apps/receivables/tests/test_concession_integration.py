from datetime import timedelta

from apps.notifications.models import Notification
from apps.sync.models import SyncRecord

from .. import adjustments, ledger, schedules
from ..models import FinanceAuditEvent, ReceivableAdjustment, StudentReceivable
from .base import ReceivablesTestCase, CLOCK

TYPE = "concession_request"
N = 100


def request(id="CNC-2026-050", **over):
    body = {"id": id, "student": "Someone", "className": "X", "type": "scholarship", "grossFee": 1, "amount": 20000,
            "reason": "Founder scholarship", "requestedBy": "Finance Office", "requestedByRole": "Finance Office",
            "requestedAt": "01 Jan 2020", "status": "pendingApproval", "decidedBy": None, "decidedAt": None, "decisionNote": None}
    body.update(over)
    return body


class ConcessionLedgerCase(ReceivablesTestCase):
    def setUp(self):
        super().setUp()
        self.finance = self.members["accountant"]
        self.authority = self.members["principal"]
        self.give_duty(self.authority)
        self.authority.user.first_name, self.authority.user.last_name = "Hauwa", "Sule"
        self.authority.user.save()
        self.bello_family()
        self.publish_bello_fees()
        self.tuition = self.charge(self.ahmad)

    def submit(self, id="CNC-2026-050", who=None, **over):
        over.setdefault("receivableId", str(self.tuition.id))
        return self.push(TYPE, id, request(id, **over), who=who or self.finance)

    def decide(self, status, id="CNC-2026-050", who=None, **over):
        stored = SyncRecord.objects.get(school=self.school, entity_type=TYPE, entity_id=id).payload
        return self.push(TYPE, id, {**stored, "status": status, **over}, operation="update", who=who or self.owner)

    def stored_request(self, id="CNC-2026-050"):
        return self.stored(TYPE, id).payload


class WhoDecidesTests(ConcessionLedgerCase):
    def test_a_delegate_with_billing_authority_decides_and_it_is_their_name_on_the_record(self):
        self.ok(self.submit())
        self.ok(self.decide("approved", who=self.authority, decisionNote="Per the founder's fund"))
        p = self.stored_request()
        self.assertEqual((p["status"], p["decidedBy"], p["decidedByRole"], p["decidedByMembershipId"]), ("approved", "Hauwa Sule", "Principal", str(self.authority.id)))

    def test_the_owner_with_no_name_on_file_is_still_recorded_as_the_proprietor(self):
        self.ok(self.submit())
        self.ok(self.decide("approved"))
        self.assertEqual(self.stored_request()["decidedBy"], "Proprietor")

    def test_the_finance_office_can_ask_but_never_decide_even_with_every_other_finance_duty(self):
        for duty in ("finance.concessions", "finance.approvals", "finance.fees", "finance.reconciliation", "finance.accounts"):
            self.give_duty(self.finance, duty=duty)
        self.ok(self.submit())
        self.rejected(self.decide("approved", who=self.finance), "can decide a concession")
        self.assertEqual(self.stored_request()["status"], "pendingApproval")

    def test_a_delegate_who_loses_the_duty_can_no_longer_decide(self):
        self.ok(self.submit())
        self.give_duty(self.authority, status="revoked")
        self.rejected(self.decide("approved", who=self.authority), "can decide a concession")

    def test_who_asked_is_the_real_person_not_a_title_the_app_typed(self):
        self.ok(self.submit(requestedBy="The Bursar"))
        p = self.stored_request()
        self.assertEqual((p["requestedByMembershipId"], p["requestedByRole"]), (str(self.finance.id), "Finance Office"))
        self.assertEqual(p["requestedByName"], "Finance Office")

    def test_delegates_receive_requests_and_are_told_of_new_ones(self):
        self.ok(self.submit())
        self.assertEqual(Notification.objects.filter(recipient=self.authority, kind="concession_request").count(), 1)
        self.assertEqual(Notification.objects.filter(recipient=self.owner, kind="concession_request").count(), 1)
        self.assertEqual(Notification.objects.filter(recipient=self.finance, kind="concession_request").count(), 0)  # not told of their own
        self.client.force_authenticate(self.authority.user)
        records = self.client.get("/api/v1/sync/pull/", {"school": str(self.school.id)}).json()["records"]
        self.assertIn(TYPE, {r["entityType"] for r in records})

    def test_the_person_who_asked_is_told_the_answer(self):
        self.ok(self.submit())
        self.ok(self.decide("declined", who=self.authority, decisionNote="Not eligible"))
        self.assertEqual(Notification.objects.filter(recipient=self.finance, kind="concession_decided").count(), 1)


class ApprovalChangesTheChargeTests(ConcessionLedgerCase):
    def test_a_request_for_a_charge_takes_its_facts_from_the_school_not_the_app(self):
        self.ok(self.submit(student="Forged Name", className="Forged Class", grossFee=999))
        p = self.stored_request()
        self.assertEqual((p["student"], p["grossFee"], p["receivableId"]), ("Ahmad Bello", 120_000, str(self.tuition.id)))
        self.assertEqual(p["className"], "JSS 1")

    def test_nothing_changes_until_it_is_approved(self):
        self.ok(self.submit())
        self.assertEqual(ledger.position(self.tuition).adjustments, 0)
        self.assertFalse(ReceivableAdjustment.objects.exists())

    def test_approving_takes_the_amount_off_the_charge_in_the_same_step(self):
        self.ok(self.submit())
        self.ok(self.decide("approved", who=self.authority))
        pos = ledger.position(StudentReceivable.objects.get(pk=self.tuition.pk))
        self.assertEqual((pos.gross, pos.adjustments, pos.net), (120_000 * N, 20_000 * N, 100_000 * N))
        adjustment = ReceivableAdjustment.objects.get()
        self.assertEqual(
            (adjustment.kind, adjustment.amount_minor, adjustment.authorized_by, adjustment.requested_by, adjustment.source_ref),
            ("scholarship", 20_000 * N, self.authority, self.finance, "concession:CNC-2026-050"),
        )
        self.assertEqual(ledger.family_position(self.family).outstanding, 280_000 * N)
        self.assertTrue(FinanceAuditEvent.objects.filter(kind="adjustment_applied", actor=self.authority).exists())
        self.assertLedgerHolds()

    def test_declining_changes_nothing(self):
        self.ok(self.submit())
        self.ok(self.decide("declined", decisionNote="Not eligible"))
        self.assertFalse(ReceivableAdjustment.objects.exists())
        self.assertEqual(ledger.family_position(self.family).outstanding, 300_000 * N)

    def test_a_discount_is_recorded_as_a_discount(self):
        self.ok(self.submit(type="discount", amount=5000))
        self.ok(self.decide("approved"))
        self.assertEqual(ReceivableAdjustment.objects.get().kind, "discount")

    def test_a_request_for_a_charge_paid_in_instalments_is_spread_across_them(self):
        due = CLOCK + timedelta(days=5)
        schedule = schedules.create_schedule(self.school, session=self.session, name="Plans", actor=self.owner)
        plan = [{"basisPoints": 5000, "dueDate": due.isoformat()}, {"basisPoints": 5000, "dueDate": (due + timedelta(days=30)).isoformat()}]
        schedules.add_item(schedule, actor=self.owner, code="PLAN", name="Plan tuition", category="tuition", amount_minor=40_000 * N, plan=plan, scope="student", student=self.maryam)
        schedules.publish(schedule, actor=self.owner)
        second = StudentReceivable.objects.get(student=self.maryam, item_code="PLAN", installment_number=2)
        self.ok(self.submit(receivableId=str(second.id), amount=10000))
        self.ok(self.decide("approved"))
        adjustments_made = ReceivableAdjustment.objects.order_by("receivable__installment_number")
        self.assertEqual([a.amount_minor for a in adjustments_made], [5_000 * N, 5_000 * N])
        self.assertEqual(len({a.group_id for a in adjustments_made}), 1)

    def test_a_request_older_style_that_names_no_charge_is_a_record_only_as_before(self):
        self.ok(self.push(TYPE, "CNC-OLD", request("CNC-OLD", student="Yusuf Bello", className="JSS 2B", grossFee=185000, amount=75000), who=self.finance))
        self.ok(self.decide("approved", id="CNC-OLD"))
        p = self.stored_request("CNC-OLD")
        self.assertEqual((p["status"], p["receivableId"], p["student"]), ("approved", None, "Yusuf Bello"))
        self.assertFalse(ReceivableAdjustment.objects.exists())


class RefusalsTests(ConcessionLedgerCase):
    def test_more_than_is_payable_on_the_charge_is_refused_at_the_request(self):
        self.rejected(self.submit(amount=120_001), "more than what is still payable")
        adjustments.adjust(self.tuition, kind="discount", amount_minor=100_000 * N, reason="Earlier", actor=self.owner)
        self.rejected(self.submit(amount=20_001), "more than what is still payable")
        self.assertFalse(SyncRecord.objects.filter(entity_type=TYPE).exists())

    def test_an_unknown_void_or_foreign_charge_is_refused(self):
        self.rejected(self.submit(receivableId="not-a-uuid"), "not valid")
        self.rejected(self.submit(receivableId="00000000-0000-0000-0000-000000000000"), "not found")
        adjustments.void_receivable(self.tuition, actor=self.owner, reason="Error")
        self.rejected(self.submit(), "voided")
        other_session, _, other_primary, _ = self.make_year(self.other_school)
        stranger = self.make_student("Zed", "Other", school=self.other_school)
        self.enroll(stranger, other_primary, other_session)
        from .. import families

        families.ensure_family_for_student(self.other_school, stranger)
        sched = schedules.create_schedule(self.other_school, session=other_session, name="Theirs", actor=self.other_owner)
        schedules.add_item(sched, actor=self.other_owner, code="X", name="X", amount_minor=50_000 * N, due_date=CLOCK + timedelta(days=5), scope="student", student=stranger)
        schedules.publish(sched, actor=self.other_owner)
        foreign = StudentReceivable.objects.get(student=stranger)
        self.rejected(self.submit(receivableId=str(foreign.id)), "not found")

    def test_if_the_charge_can_no_longer_take_it_the_approval_is_undone_with_it(self):
        self.ok(self.submit())
        adjustments.adjust(self.tuition, kind="discount", amount_minor=110_000 * N, reason="Bigger award since", actor=self.owner)
        response = self.decide("approved")
        self.rejected(response, "more than the")
        self.assertEqual(self.stored_request()["status"], "pendingApproval")  # still waiting: nothing half-done
        self.assertEqual(ReceivableAdjustment.objects.count(), 1)

    def test_a_charge_voided_after_the_request_undoes_the_approval_too(self):
        self.ok(self.submit())
        adjustments.void_receivable(self.tuition, actor=self.owner, reason="Raised in error")
        self.rejected(self.decide("approved"), "voided")
        self.assertEqual(self.stored_request()["status"], "pendingApproval")

    def test_a_decision_is_final_and_cannot_be_applied_twice(self):
        self.ok(self.submit())
        self.ok(self.decide("approved"))
        self.rejected(self.decide("approved"), "already approved")
        self.assertEqual(ReceivableAdjustment.objects.count(), 1)
