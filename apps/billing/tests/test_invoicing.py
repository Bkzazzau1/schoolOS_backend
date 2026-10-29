"""Usage-meter publication and invoice generation: the existing per-student calculation, exactly as
it stands today, plus the idempotency guarantees that make the automatic cycle runner and the
console's manual issue-latest-invoice path both safe to retry."""

from django.utils import timezone

from apps.billing.cycle import BillingCycleNotReady, run_billing_cycles, run_subscription_cycle
from apps.billing.models import BillingInvoice, InvoiceStatus, Plan, SchoolBillingMeterSnapshot
from apps.billing.services import admin_set_plan, capture_usage_snapshot, issue_latest_usage_invoice
from apps.organizations.services import create_organization, provision_school
from apps.schools.models import SchoolType

from .base import BillingTestCase, verified_user


class MeterPublicationTests(BillingTestCase):
    def test_publishing_counts_only_active_billable_enrollments(self):
        self.add_billable_students(self.school, 5)
        self.add_billable_students(self.school, 2, is_billable=False)
        snapshot = self.publish_meter()
        self.assertEqual(snapshot.billable_student_count, 5)
        self.assertTrue(snapshot.authoritative)
        self.assertEqual(snapshot.source, "canonical_student_roster")

    def test_replaying_the_same_source_version_is_idempotent(self):
        self.add_billable_students(self.school, 3)
        first = self.publish_meter(observation_key="same-key")
        second = self.publish_meter(observation_key="same-key")
        self.assertEqual(first.id, second.id)
        self.assertEqual(SchoolBillingMeterSnapshot.objects.filter(school=self.school).count(), 2)  # provision bootstrap + this one

    def test_a_different_count_under_the_same_source_version_is_refused_not_silently_overwritten(self):
        self.add_billable_students(self.school, 3)
        self.publish_meter(observation_key="fixed-key")
        self.add_billable_students(self.school, 1)
        with self.assertRaises(ValueError):
            self.publish_meter(observation_key="fixed-key")


class InvoiceGenerationTests(BillingTestCase):
    def _ready_period(self):
        run_subscription_cycle(self.subscription.id)
        self.publish_meter()
        cutoff = timezone.now()
        self.subscription.refresh_from_db()
        self.subscription.current_period_start = cutoff - timezone.timedelta(hours=1)
        self.subscription.current_period_end = cutoff
        self.subscription.save(update_fields=["current_period_start", "current_period_end"])

    def test_new_invoices_are_priced_by_fixed_bracket_not_per_student(self):
        # base_amount_minor/student_unit_amount_minor are set deliberately: if either still
        # influenced the amount, this would fail - the fixed-bracket fee is all that matters now.
        self.set_plan(base_amount_minor=1_000_000, student_unit_amount_minor=50_000)
        self.add_billable_students(self.school, 12)  # within the 1-100 bracket: NGN40,000
        self._ready_period()
        run_subscription_cycle(self.subscription.id)
        invoice = self.subscription.invoices.get()
        self.assertEqual(invoice.billable_student_count, 12)
        self.assertEqual(invoice.amount_due_minor, 4_000_000)
        self.assertEqual(invoice.status, InvoiceStatus.OPEN)
        self.assertEqual(invoice.metadata["pricingMode"], "fixed_bracket")
        line = invoice.lines.get()
        self.assertEqual((line.school_id, line.billable_student_count, line.fixed_fee_minor, line.pricing_source), (self.school.id, 12, 4_000_000, "bracket"))

    def test_a_school_with_zero_billable_students_is_never_silently_invoiced(self):
        self.set_plan(base_amount_minor=1_000_000)
        self._ready_period()  # no students added; the school's authoritative count is 0
        with self.assertRaises(BillingCycleNotReady):
            run_subscription_cycle(self.subscription.id)
        self.assertFalse(BillingInvoice.objects.filter(subscription=self.subscription).exists())

    def test_one_usage_snapshot_can_never_produce_two_invoices(self):
        self.set_plan(base_amount_minor=1_000_000)
        self.add_billable_students(self.school, 6)
        self._ready_period()
        run_subscription_cycle(self.subscription.id)
        first = self.subscription.invoices.get()
        snapshot = first.usage_snapshot
        from apps.billing.cycle import _issue_period_invoice_locked, _policy_for

        again = _issue_period_invoice_locked(self.subscription, snapshot, policy=_policy_for(self.subscription), now=timezone.now())
        self.assertEqual(again.id, first.id)
        self.assertEqual(BillingInvoice.objects.filter(subscription=self.subscription).count(), 1)

    def test_a_retried_cycle_run_for_the_same_period_is_a_no_op_not_a_second_invoice(self):
        self.set_plan(base_amount_minor=1_000_000)
        self.add_billable_students(self.school, 6)
        self._ready_period()
        result_a = run_subscription_cycle(self.subscription.id)
        self.assertEqual(result_a["result"], "invoice_issued")
        # The invoice just issued is still open (unpaid), so a second call right away correctly
        # reports it as outstanding rather than advancing to, or billing, a further period.
        result_b = run_subscription_cycle(self.subscription.id)
        self.assertEqual(result_b["result"], "invoice_outstanding")
        self.assertEqual(BillingInvoice.objects.filter(subscription=self.subscription).count(), 1)

    def test_the_cycle_refuses_to_bill_a_period_whose_schools_are_not_all_metered_yet(self):
        self.set_plan(base_amount_minor=1_000_000)
        provision_school(
            actor=self.owner, organization_id=self.organization.id, name="Second Campus",
            school_type=SchoolType.PRIMARY, location="Kano",
        )  # its bootstrap zero-student meter is measured now, BEFORE period_start below
        run_subscription_cycle(self.subscription.id)  # initializes the period
        period_start = timezone.now()
        self.publish_meter(self.school)  # only this school's meter falls inside the period window
        period_end = timezone.now()
        self.subscription.refresh_from_db()
        self.subscription.current_period_start = period_start
        self.subscription.current_period_end = period_end
        self.subscription.save(update_fields=["current_period_start", "current_period_end"])
        with self.assertRaises(BillingCycleNotReady):
            run_subscription_cycle(self.subscription.id)
        self.assertFalse(BillingInvoice.objects.filter(subscription=self.subscription).exists())

    def test_console_issue_latest_invoice_is_refused_while_automatic_invoicing_owns_the_plan(self):
        self.set_plan(base_amount_minor=1_000_000, automatic=True)
        self.add_billable_students(self.school, 3)
        self.publish_meter()
        response = self.post(f"organizations/{self.organization.id}/billing/invoices/", who=self.billing_administrator())
        self.assertEqual(response.status_code, 400, response.json())

    def test_console_issue_latest_invoice_works_from_the_manual_path_when_automatic_invoicing_is_off(self):
        self.set_plan(base_amount_minor=1_000_000, automatic=False)
        self.add_billable_students(self.school, 3)
        capture_usage_snapshot(self.organization, billable_student_count=3, source="manual_test")
        invoice = issue_latest_usage_invoice(self.organization)
        self.assertEqual((invoice.billable_student_count, invoice.amount_due_minor), (3, 4_000_000))  # 1-100 bracket
        # Calling it again for the same (only) snapshot returns the same invoice, never a duplicate.
        again = issue_latest_usage_invoice(self.organization)
        self.assertEqual(again.id, invoice.id)

    def test_invoices_are_only_visible_to_this_organizations_own_billing_authority(self):
        self.set_plan(base_amount_minor=1_000_000, automatic=False)
        capture_usage_snapshot(self.organization, billable_student_count=10, source="manual_test")
        issue_latest_usage_invoice(self.organization)
        stranger = verified_user("stranger@example.ng")
        response = self.get(f"organizations/{self.organization.id}/billing/invoices/", who=stranger)
        self.assertEqual(response.status_code, 403)
        teacher_role_user = self.billing_administrator()  # a real org member, but not billing authority by default role check below
        from apps.organizations.models import OrganizationMembership, OrganizationRole

        OrganizationMembership.objects.filter(user=teacher_role_user, organization=self.organization).update(role=OrganizationRole.ADMINISTRATOR)
        response = self.get(f"organizations/{self.organization.id}/billing/invoices/", who=teacher_role_user)
        self.assertEqual(response.status_code, 403)  # administrator manages schools, not billing
        ok = self.get(f"organizations/{self.organization.id}/billing/invoices/")  # the owner always can
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(len(ok.json()["invoices"]), 1)


class BillingCyclesBatchDriverTests(BillingTestCase):
    def test_run_billing_cycles_processes_every_active_organization_and_reports_real_counts(self):
        self.set_plan(base_amount_minor=1_000_000)
        self.add_billable_students(self.school, 2)
        run_subscription_cycle(self.subscription.id)
        self.publish_meter()
        cutoff = timezone.now()
        self.subscription.refresh_from_db()
        self.subscription.current_period_start = cutoff - timezone.timedelta(hours=1)
        self.subscription.current_period_end = cutoff
        self.subscription.save(update_fields=["current_period_start", "current_period_end"])

        other_owner = verified_user("other-cycle-owner@example.ng")
        other_org, _ = create_organization(actor=other_owner, name="No Cadence Yet Org")
        no_cadence_plan = Plan.objects.create(code="no-cadence", name="No Cadence Configured")
        admin_set_plan(other_org.subscription, no_cadence_plan, actor_user=other_owner, source="test")

        summary = run_billing_cycles()
        self.assertEqual(summary["processed"], 2)
        self.assertEqual(summary["invoicesIssued"], 1)
        self.assertEqual(summary["skipped"], 1)  # the unconfigured organization, safely skipped, not errored
        self.assertEqual(summary["errors"], [])
        self.assertEqual(BillingInvoice.objects.filter(organization=self.organization).count(), 1)
        self.assertFalse(BillingInvoice.objects.filter(organization=other_org).exists())

    def test_running_the_whole_batch_twice_in_a_row_never_double_invoices(self):
        self.set_plan(base_amount_minor=1_000_000)
        self.add_billable_students(self.school, 2)
        run_subscription_cycle(self.subscription.id)
        self.publish_meter()
        cutoff = timezone.now()
        self.subscription.refresh_from_db()
        self.subscription.current_period_start = cutoff - timezone.timedelta(hours=1)
        self.subscription.current_period_end = cutoff
        self.subscription.save(update_fields=["current_period_start", "current_period_end"])

        run_billing_cycles()
        run_billing_cycles()
        self.assertEqual(BillingInvoice.objects.filter(organization=self.organization).count(), 1)
