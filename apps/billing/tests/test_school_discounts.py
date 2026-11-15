"""Operator-negotiated school discounts: a new commercial lever, separate from the
automatic 10% annual-payment discount every annual payer already gets. Covers creation
validation, time-windowed activation (the same pattern as PricingBracket/EnterpriseAgreement),
never going negative, and correct composition with the annual discount - a fixed-amount
discount must reduce the final invoice by that flat amount once, never multiplied by a
term count it was never meant to scale with."""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from apps.academics.models import AcademicSession, AcademicTerm
from apps.organizations.services import create_organization, provision_school
from apps.schools.models import SchoolType

from ..models import DiscountKind, PricingAuditEvent
from ..pricing import (
    active_school_discount_for,
    create_school_discount,
    end_school_discount,
    fixed_fee_for_school,
    priced_fee_for_school,
)
from .base import verified_user


def _provision_school(name="BrightGate Academy"):
    owner = verified_user(f"owner-{name.lower().replace(' ', '-')}@example.ng")
    organization, _ = create_organization(actor=owner, name=f"{name} Group")
    school, _ = provision_school(actor=owner, organization_id=organization.id, name=name, school_type=SchoolType.SECONDARY, location="Kaduna")
    return school


def _make_session_with_terms(school, *, term_count, starts_on=None):
    starts_on = starts_on or (timezone.localdate() - timedelta(days=30))
    session = AcademicSession.objects.create(
        school=school, code=f"session-{term_count}", name=f"Session ({term_count} terms)",
        starts_on=starts_on, ends_on=starts_on + timedelta(days=270),
    )
    cursor = starts_on
    for sequence in range(1, term_count + 1):
        term_end = cursor + timedelta(days=80)
        AcademicTerm.objects.create(
            session=session, code=f"t{sequence}", name=f"Term {sequence}", sequence=sequence,
            starts_on=cursor, ends_on=term_end,
        )
        cursor = term_end + timedelta(days=10)
    return session


class CreateSchoolDiscountValidationTests(TestCase):
    def test_a_reason_is_required(self):
        school = _provision_school()
        with self.assertRaises(ValueError):
            create_school_discount(school=school, kind=DiscountKind.PERCENT, percent_off=10, reason="   ")

    def test_percent_off_must_be_between_1_and_100(self):
        school = _provision_school()
        for bad in (0, 101, None):
            with self.assertRaises(ValueError):
                create_school_discount(school=school, kind=DiscountKind.PERCENT, percent_off=bad, reason="deal")

    def test_amount_off_must_be_positive(self):
        school = _provision_school()
        for bad in (0, -1, None):
            with self.assertRaises(ValueError):
                create_school_discount(school=school, kind=DiscountKind.FIXED, amount_off_minor=bad, reason="goodwill")

    def test_an_unknown_kind_is_refused(self):
        school = _provision_school()
        with self.assertRaises(ValueError):
            create_school_discount(school=school, kind="free_forever", reason="nope")

    def test_creating_one_is_audited(self):
        school = _provision_school()
        discount = create_school_discount(
            school=school, kind=DiscountKind.PERCENT, percent_off=15, reason="renewal deal",
        )
        event = PricingAuditEvent.objects.get(action="school_discount_created", target_id=str(discount.id))
        self.assertEqual(event.detail["schoolId"], str(school.id))
        self.assertEqual(event.detail["percentOff"], 15)
        self.assertEqual(event.detail["reason"], "renewal deal")


class ActiveSchoolDiscountTests(TestCase):
    def test_a_discount_with_no_end_date_is_active(self):
        school = _provision_school()
        discount = create_school_discount(school=school, kind=DiscountKind.PERCENT, percent_off=10, reason="deal")
        self.assertEqual(active_school_discount_for(school).id, discount.id)

    def test_an_ended_discount_is_no_longer_active(self):
        school = _provision_school()
        discount = create_school_discount(school=school, kind=DiscountKind.PERCENT, percent_off=10, reason="deal")
        end_school_discount(discount)
        self.assertIsNone(active_school_discount_for(school))

    def test_ending_an_already_ended_discount_is_refused(self):
        school = _provision_school()
        discount = create_school_discount(school=school, kind=DiscountKind.PERCENT, percent_off=10, reason="deal")
        end_school_discount(discount)
        with self.assertRaises(ValueError):
            end_school_discount(discount)

    def test_the_newest_discount_wins_when_more_than_one_exists(self):
        school = _provision_school()
        now = timezone.now()
        create_school_discount(
            school=school, kind=DiscountKind.PERCENT, percent_off=10, reason="old deal",
            effective_from=now - timedelta(days=30),
        )
        newer = create_school_discount(
            school=school, kind=DiscountKind.FIXED, amount_off_minor=500_000, reason="better deal",
            effective_from=now - timedelta(days=1),
        )
        self.assertEqual(active_school_discount_for(school).id, newer.id)

    def test_a_school_with_no_discount_has_none_active(self):
        school = _provision_school()
        self.assertIsNone(active_school_discount_for(school))


class PricedFeeForSchoolTests(TestCase):
    def test_with_no_discount_the_price_is_unchanged(self):
        school = _provision_school()
        plain = fixed_fee_for_school(school, billable_student_count=50)
        priced = priced_fee_for_school(school, billable_student_count=50)
        self.assertEqual(priced.fixed_fee_minor, plain.fixed_fee_minor)
        self.assertEqual(priced.discount_minor, 0)
        self.assertEqual(priced.gross_fee_minor, plain.fixed_fee_minor)

    def test_a_percent_discount_reduces_the_fee_and_is_reflected_in_discount_minor(self):
        school = _provision_school()
        create_school_discount(school=school, kind=DiscountKind.PERCENT, percent_off=20, reason="deal")
        result = priced_fee_for_school(school, billable_student_count=50)  # 1-100 bracket: 4,000,000
        self.assertEqual(result.gross_fee_minor, 4_000_000)
        self.assertEqual(result.discount_minor, 800_000)
        self.assertEqual(result.fixed_fee_minor, 3_200_000)
        self.assertEqual(result.fixed_fee_minor + result.discount_minor, result.gross_fee_minor)

    def test_a_fixed_discount_larger_than_the_fee_never_goes_negative(self):
        school = _provision_school()
        create_school_discount(school=school, kind=DiscountKind.FIXED, amount_off_minor=99_000_000, reason="deal")
        result = priced_fee_for_school(school, billable_student_count=50)  # 4,000,000 fee
        self.assertEqual(result.fixed_fee_minor, 0)
        self.assertEqual(result.discount_minor, 4_000_000)

    def test_an_ended_discount_no_longer_applies(self):
        school = _provision_school()
        discount = create_school_discount(school=school, kind=DiscountKind.PERCENT, percent_off=50, reason="deal")
        end_school_discount(discount)
        result = priced_fee_for_school(school, billable_student_count=50)
        self.assertEqual(result.fixed_fee_minor, 4_000_000)
        self.assertEqual(result.discount_minor, 0)

    def test_a_fixed_discount_combines_with_the_annual_discount_without_being_multiplied_by_term_count(self):
        school = _provision_school()
        _make_session_with_terms(school, term_count=3)
        create_school_discount(school=school, kind=DiscountKind.FIXED, amount_off_minor=500_000, reason="deal")

        result = priced_fee_for_school(school, billable_student_count=50, annual=True)
        # 1-100 bracket = 4,000,000/term * 3 terms = 12,000,000 gross; 10% annual discount
        # nets 10,800,000 before the operator discount is applied on top, once - never 3x.
        self.assertEqual(result.gross_fee_minor, 12_000_000)
        self.assertEqual(result.fixed_fee_minor, 10_800_000 - 500_000)
        self.assertEqual(result.discount_minor, 1_200_000 + 500_000)
        self.assertEqual(result.fixed_fee_minor + result.discount_minor, result.gross_fee_minor)
