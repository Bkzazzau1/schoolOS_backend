"""Shared fixtures for apps.billing's own test suite: a real organization, a real school under it,
a billing plan with an automatic-invoicing policy, and a way to give that school a real, authoritative
student count through the same canonical path apps.students itself publishes through - never a
shortcut that bypasses the tamper-resistance this app relies on."""

import itertools
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.organizations.models import Organization, OrganizationMembership, OrganizationRole
from apps.organizations.services import create_organization, provision_school
from apps.schools.models import SchoolType
from apps.students.models import EnrollmentStatus, Student, StudentEnrollment
from apps.students.services import publish_school_roster_meter

from ..models import BillingCyclePolicy, BillingInterval, Plan, PlanEntitlement

User = get_user_model()
PASSWORD = "a-long-test-password-1"
_numbers = itertools.count(1)


def verified_user(email, **extra):
    user = User.objects.create_user(email, PASSWORD, **extra)
    user.email_verified_at = timezone.now()
    user.save(update_fields=["email_verified_at"])
    return user


class BillingTestCase(APITestCase):
    def setUp(self):
        self.owner = verified_user("owner@example.ng", first_name="Amina")
        self.organization, self.owner_membership = create_organization(actor=self.owner, name="BrightGate Group")
        self.school, self.proprietor_membership = provision_school(
            actor=self.owner, organization_id=self.organization.id, name="BrightGate Academy",
            school_type=SchoolType.SECONDARY, location="Kaduna",
        )
        self.subscription = self.organization.subscription

    # -- organization-level roles --------------------------------------------------------------

    def billing_administrator(self):
        user = verified_user(f"billing{next(_numbers)}@example.ng")
        OrganizationMembership.objects.create(user=user, organization=self.organization, role=OrganizationRole.BILLING_ADMINISTRATOR)
        return user

    # -- plan / policy --------------------------------------------------------------------------

    def set_plan(self, *, interval=BillingInterval.MONTHLY, base_amount_minor=0, student_unit_amount_minor=0, automatic=True, invoice_due_days=7, past_due_days=3, grace_days=7):
        plan = self.subscription.plan
        plan.billing_interval = interval
        plan.base_amount_minor = base_amount_minor
        plan.student_unit_amount_minor = student_unit_amount_minor
        plan.save(update_fields=["billing_interval", "base_amount_minor", "student_unit_amount_minor"])
        BillingCyclePolicy.objects.update_or_create(
            plan=plan,
            defaults={
                "automatic_invoicing_enabled": automatic,
                "invoice_due_days": invoice_due_days,
                "past_due_days": past_due_days,
                "grace_days": grace_days,
            },
        )
        self.subscription.refresh_from_db()
        return plan

    def give_entitlement(self, code, *, enabled=True, limit_value=None):
        PlanEntitlement.objects.update_or_create(
            plan=self.subscription.plan, code=code, defaults={"enabled": enabled, "limit_value": limit_value},
        )

    # -- students / metering --------------------------------------------------------------------

    def add_billable_students(self, school, count, *, is_billable=True):
        now = timezone.now()
        for _ in range(count):
            n = next(_numbers)
            student = Student.objects.create(school=school, student_code=f"STU-{n:06d}", admission_number=f"ADM-{n:06d}", first_name="Test", surname=f"Student{n}")
            StudentEnrollment.objects.create(
                school=school, student=student, academic_section="Secondary", class_name="JSS 1",
                status=EnrollmentStatus.ACTIVE, is_billable=is_billable, started_at=now,
            )

    def publish_meter(self, school=None, *, observation_key=None, bump_revision=False):
        return publish_school_roster_meter(
            school or self.school, observation_key=observation_key or f"test:{next(_numbers)}", bump_revision=bump_revision,
        )

    # -- http ---------------------------------------------------------------------------------

    def url(self, tail: str) -> str:
        return f"/api/v1/{tail}"

    def get(self, tail, *, who=None):
        self.client.force_authenticate(who or self.owner)
        return self.client.get(self.url(tail))

    def post(self, tail, data=None, *, who=None):
        self.client.force_authenticate(who or self.owner)
        return self.client.post(self.url(tail), data or {}, format="json")
