"""Subscription initialization, plan assignment, entitlements, and lifecycle-status transitions
(trial expiry, past-due/grace/restriction, restoration after payment) - the part of apps.billing
that gates access to the whole platform (apps.core.tests.test_subscription_access already proves the
*enforcement* side from apps.core; this proves the *source of truth* these checks read from)."""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.organizations.models import OrganizationRole
from apps.organizations.services import create_organization, provision_school
from apps.schools.models import SchoolType

from ..cycle import BillingCycleNotReady, run_subscription_cycle
from ..models import InvoiceStatus, OrganizationSubscription, SubscriptionEvent, SubscriptionStatus
from ..services import (
    admin_set_plan,
    admin_set_status,
    baseline_plan,
    ensure_organization_subscription,
    require_entitlement,
    require_school_provisioning,
    resolved_entitlements,
    transition_subscription,
)
from .base import BillingTestCase, verified_user

User = get_user_model()


class SubscriptionInitializationTests(BillingTestCase):
    def test_a_new_organization_gets_a_real_active_subscription_on_the_baseline_plan(self):
        self.assertEqual(self.subscription.status, SubscriptionStatus.ACTIVE)
        self.assertEqual(self.subscription.plan.code, baseline_plan().code)
        self.assertTrue(SubscriptionEvent.objects.filter(subscription=self.subscription, event="subscription_created").exists())

    def test_calling_ensure_twice_never_creates_a_second_subscription(self):
        again = ensure_organization_subscription(self.organization)
        self.assertEqual(again.id, self.subscription.id)
        self.assertEqual(OrganizationSubscription.objects.filter(organization=self.organization).count(), 1)

    def test_the_api_reports_the_real_subscription_to_a_member_and_refuses_an_outsider(self):
        body = self.get(f"organizations/{self.organization.id}/subscription/").json()
        self.assertEqual((body["status"], body["accessMode"]), ("active", "full"))
        stranger = verified_user("stranger@example.ng")
        response = self.get(f"organizations/{self.organization.id}/subscription/", who=stranger)
        self.assertEqual(response.status_code, 403)


class PlanAssignmentTests(BillingTestCase):
    def test_an_operator_can_assign_a_different_plan_and_it_is_audited(self):
        from ..models import Plan

        other = Plan.objects.create(code="premium", name="SkulGen Premium")
        operator = User.objects.create_superuser("ops@skulgen.app", "a-long-test-password-2")
        admin_set_plan(self.subscription, other, actor_user=operator, source="console")
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.plan_id, other.id)
        event = SubscriptionEvent.objects.get(event="admin_plan_changed")
        self.assertEqual((event.detail["adminEmail"], event.detail["toPlanId"]), (operator.email, str(other.id)))

    def test_entitlements_resolve_per_plan_and_are_unavailable_when_access_is_not_full(self):
        self.give_entitlement("multi_school", enabled=True, limit_value=1)
        resolved = resolved_entitlements(self.subscription)
        self.assertEqual((resolved["multi_school"]["enabled"], resolved["multi_school"]["available"]), (True, True))
        admin_set_status(self.subscription, SubscriptionStatus.SUSPENDED, actor_user=User.objects.create_superuser("ops2@skulgen.app", "a-long-test-password-2"), source="console")
        self.subscription.refresh_from_db()
        self.assertFalse(resolved_entitlements(self.subscription)["multi_school"]["available"])

    def test_require_entitlement_enforces_its_own_limit(self):
        self.give_entitlement("multi_school", enabled=True, limit_value=1)
        require_entitlement(self.organization, "multi_school", current_value=0)
        with self.assertRaises(PermissionDenied):
            require_entitlement(self.organization, "multi_school", current_value=1)

    def test_require_entitlement_refuses_a_disabled_capability(self):
        self.give_entitlement("multi_school", enabled=False)
        with self.assertRaises(PermissionDenied):
            require_entitlement(self.organization, "multi_school")

    def test_require_school_provisioning_gates_a_second_school_on_the_multi_school_entitlement(self):
        self.give_entitlement("school_provisioning", enabled=True, limit_value=None)
        self.give_entitlement("multi_school", enabled=False)
        require_school_provisioning(self.organization, current_school_count=0)  # the first school is always fine
        with self.assertRaises(PermissionDenied):
            require_school_provisioning(self.organization, current_school_count=1)


class StatusTransitionTests(BillingTestCase):
    def test_transition_subscription_changes_status_and_writes_one_event(self):
        transition_subscription(self.subscription, status=SubscriptionStatus.PAST_DUE, event="invoice_past_due")
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, SubscriptionStatus.PAST_DUE)
        event = SubscriptionEvent.objects.get(subscription=self.subscription, event="invoice_past_due")
        self.assertEqual((event.from_status, event.to_status), (SubscriptionStatus.ACTIVE, SubscriptionStatus.PAST_DUE))

    def test_transitioning_to_the_same_status_writes_no_redundant_status_change_but_still_logs_the_event(self):
        before = SubscriptionEvent.objects.filter(subscription=self.subscription).count()
        transition_subscription(self.subscription, status=SubscriptionStatus.ACTIVE, event="noop_check")
        self.assertEqual(SubscriptionEvent.objects.filter(subscription=self.subscription).count(), before + 1)

    def test_an_unknown_status_is_refused(self):
        with self.assertRaises(ValueError):
            transition_subscription(self.subscription, status="not_a_real_status", event="x")

    def test_admin_set_status_is_audited_with_who_and_where_from(self):
        operator = User.objects.create_superuser("ops3@skulgen.app", "a-long-test-password-2")
        admin_set_status(self.subscription, SubscriptionStatus.RESTRICTED, actor_user=operator, source="console")
        event = SubscriptionEvent.objects.get(event="admin_status_changed")
        self.assertEqual((event.detail["adminEmail"], event.detail["source"]), (operator.email, "console"))

    def test_an_invalid_admin_status_is_refused(self):
        operator = User.objects.create_superuser("ops4@skulgen.app", "a-long-test-password-2")
        with self.assertRaises(ValidationError):
            admin_set_status(self.subscription, "not_real", actor_user=operator, source="console")


class TrialExpiryAndCycleTests(BillingTestCase):
    def test_a_trial_that_has_not_ended_yet_is_left_alone_by_the_cycle_runner(self):
        self.set_plan()
        self.subscription.status = SubscriptionStatus.TRIAL
        self.subscription.trial_ends_at = timezone.now() + timedelta(days=5)
        self.subscription.save(update_fields=["status", "trial_ends_at"])
        result = run_subscription_cycle(self.subscription.id)
        self.assertEqual(result["result"], "trial")
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, SubscriptionStatus.TRIAL)

    def test_a_trial_past_its_end_date_becomes_active_and_the_cycle_continues(self):
        self.set_plan()
        self.add_billable_students(self.school, 10)
        self.publish_meter()
        self.subscription.status = SubscriptionStatus.TRIAL
        self.subscription.trial_ends_at = timezone.now() - timedelta(days=1)
        self.subscription.save(update_fields=["status", "trial_ends_at"])
        result = run_subscription_cycle(self.subscription.id)
        self.subscription.refresh_from_db()
        self.assertNotEqual(self.subscription.status, SubscriptionStatus.TRIAL)
        self.assertIn(result["result"], ("invoice_issued", "scheduled"))
        self.assertTrue(SubscriptionEvent.objects.filter(subscription=self.subscription, event="trial_ended").exists())

    def test_the_cycle_runner_refuses_to_guess_when_the_plan_has_no_cadence_configured(self):
        with self.assertRaises(BillingCycleNotReady):
            run_subscription_cycle(self.subscription.id)

    def test_the_cycle_runner_refuses_to_guess_when_the_policy_is_incomplete(self):
        self.set_plan(automatic=True, invoice_due_days=None)
        with self.assertRaises(BillingCycleNotReady):
            run_subscription_cycle(self.subscription.id)

    def test_past_due_then_grace_then_restriction_follow_the_configured_day_counts(self):
        self.set_plan(past_due_days=0, grace_days=0, base_amount_minor=4_000_000)
        self.add_billable_students(self.school, 5)
        run_subscription_cycle(self.subscription.id)  # initializes the period first
        self.publish_meter()  # measured just now, inside the one-hour window set below
        cutoff = timezone.now()
        self.subscription.refresh_from_db()
        self.subscription.current_period_start = cutoff - timedelta(hours=1)
        self.subscription.current_period_end = cutoff
        self.subscription.save(update_fields=["current_period_start", "current_period_end"])
        run_subscription_cycle(self.subscription.id)  # issues the invoice, due immediately-ish
        invoice = self.subscription.invoices.get()
        invoice.due_at = timezone.now() - timedelta(minutes=1)
        invoice.save(update_fields=["due_at"])
        run_subscription_cycle(self.subscription.id)
        self.subscription.refresh_from_db()
        invoice.refresh_from_db()
        self.assertEqual(self.subscription.status, SubscriptionStatus.RESTRICTED)
        self.assertEqual(invoice.status, InvoiceStatus.OPEN)

    def test_paying_the_overdue_invoice_restores_the_subscription_to_active(self):
        self.set_plan(past_due_days=0, grace_days=0, base_amount_minor=4_000_000)
        self.add_billable_students(self.school, 5)
        run_subscription_cycle(self.subscription.id)  # initializes the period first
        self.publish_meter()  # measured just now, inside the one-hour window set below
        cutoff = timezone.now()
        self.subscription.refresh_from_db()
        self.subscription.current_period_start = cutoff - timedelta(hours=1)
        self.subscription.current_period_end = cutoff
        self.subscription.save(update_fields=["current_period_start", "current_period_end"])
        run_subscription_cycle(self.subscription.id)
        invoice = self.subscription.invoices.get()
        invoice.due_at = timezone.now() - timedelta(minutes=1)
        invoice.save(update_fields=["due_at"])
        run_subscription_cycle(self.subscription.id)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, SubscriptionStatus.RESTRICTED)

        invoice.status = InvoiceStatus.PAID
        invoice.amount_paid_minor = invoice.amount_due_minor
        invoice.save(update_fields=["status", "amount_paid_minor"])
        run_subscription_cycle(self.subscription.id)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, SubscriptionStatus.ACTIVE)

    def test_one_organizations_billing_trouble_never_touches_a_sibling_organizations_subscription(self):
        other_owner = verified_user("other-owner@example.ng")
        other_org, _ = create_organization(actor=other_owner, name="Unrelated Group")
        other_sub = other_org.subscription
        operator = User.objects.create_superuser("ops5@skulgen.app", "a-long-test-password-2")
        admin_set_status(self.subscription, SubscriptionStatus.SUSPENDED, actor_user=operator, source="console")
        other_sub.refresh_from_db()
        self.assertEqual(other_sub.status, SubscriptionStatus.ACTIVE)
