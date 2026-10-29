"""Fixed-bracket pricing (Phase C, priority two): a school pays the fixed fee for its
enrollment bracket, never an amount computed by multiplying students by a unit price. These
tests cover the pricing module in isolation - exact bracket boundaries, zero enrollment,
enterprise accounts, overlap prevention, and that changing prices or adding brackets never
reaches back and changes a fee already determined for an earlier period."""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from apps.organizations.services import create_organization, provision_school
from apps.schools.models import SchoolType

from ..models import PricingBracket
from ..pricing import (
    OverlappingBracketError,
    PricingPolicyUndefined,
    active_bracket_for,
    create_enterprise_agreement,
    create_pricing_bracket,
    fixed_fee_for_school,
)
from .base import verified_user


def _provision_school(name="BrightGate Academy"):
    owner = verified_user(f"owner-{name.lower().replace(' ', '-')}@example.ng")
    organization, _ = create_organization(actor=owner, name=f"{name} Group")
    school, _ = provision_school(actor=owner, organization_id=organization.id, name=name, school_type=SchoolType.SECONDARY, location="Kaduna")
    return school

# The migration-seeded approved table; asserted here so a change to the seed data is a visible,
# deliberate test failure rather than a silent drift from the user's approved commercial table.
APPROVED_BRACKETS = [
    (1, 100, 4_000_000),
    (101, 300, 7_500_000),
    (301, 600, 12_000_000),
    (601, 1_000, 18_000_000),
    (1_001, 2_000, 25_000_000),
]


class SeededBracketTableTests(TestCase):
    def test_the_migration_seeds_exactly_the_approved_table(self):
        for min_students, max_students, fixed_fee_minor in APPROVED_BRACKETS:
            bracket = active_bracket_for(min_students)
            self.assertIsNotNone(bracket, (min_students, max_students))
            self.assertEqual((bracket.min_students, bracket.max_students, bracket.fixed_fee_minor), (min_students, max_students, fixed_fee_minor))


class BracketBoundaryTests(TestCase):
    def test_every_approved_boundary_resolves_to_the_correct_fee(self):
        school = _provision_school()
        cases = [
            (1, 4_000_000), (80, 4_000_000), (100, 4_000_000),
            (101, 7_500_000), (250, 7_500_000), (300, 7_500_000),
            (301, 12_000_000), (450, 12_000_000), (600, 12_000_000),
            (601, 18_000_000), (800, 18_000_000), (1_000, 18_000_000),
            (1_001, 25_000_000), (1_500, 25_000_000), (2_000, 25_000_000),
        ]
        for count, expected_fee in cases:
            result = fixed_fee_for_school(school, billable_student_count=count)
            self.assertEqual(result.fixed_fee_minor, expected_fee, count)
            self.assertEqual(result.source, "bracket")

    def test_zero_billable_students_is_an_explicit_undefined_policy_not_the_first_bracket(self):
        school = _provision_school()
        with self.assertRaises(PricingPolicyUndefined):
            fixed_fee_for_school(school, billable_student_count=0)

    def test_above_the_highest_bracket_with_no_enterprise_agreement_is_undefined_not_auto_priced(self):
        school = _provision_school()
        with self.assertRaises(PricingPolicyUndefined):
            fixed_fee_for_school(school, billable_student_count=2_001)

    def test_an_authorized_enterprise_agreement_prices_a_school_above_2000(self):
        school = _provision_school()
        agreement = create_enterprise_agreement(
            school=school, fixed_fee_minor=40_000_000, effective_from=timezone.now() - timedelta(days=1),
        )
        result = fixed_fee_for_school(school, billable_student_count=3_500)
        self.assertEqual((result.fixed_fee_minor, result.source, result.enterprise_agreement_id), (40_000_000, "enterprise", str(agreement.id)))

    def test_an_enterprise_agreement_never_applies_to_another_school(self):
        priced_school = _provision_school("Priced Academy")
        other_school = _provision_school("Other Academy")
        create_enterprise_agreement(school=priced_school, fixed_fee_minor=40_000_000, effective_from=timezone.now() - timedelta(days=1))
        with self.assertRaises(PricingPolicyUndefined):
            fixed_fee_for_school(other_school, billable_student_count=3_000)


class BracketVersioningAndOverlapTests(TestCase):
    def setUp(self):
        # The migration-seeded approved table would otherwise overlap every bracket these tests
        # create of their own; these tests are about the overlap/versioning rules themselves; the
        # seeded table is already proven correct by SeededBracketTableTests and BracketBoundaryTests.
        PricingBracket.objects.all().delete()

    def test_changing_a_brackets_price_never_changes_what_an_earlier_period_resolves_to(self):
        school = _provision_school()
        before_change = timezone.now() - timedelta(days=10)
        bracket_a = create_pricing_bracket(min_students=1, max_students=100, fixed_fee_minor=4_000_000, effective_from=before_change - timedelta(days=365))
        bracket_a.effective_until = timezone.now()
        bracket_a.save(update_fields=["effective_until"])
        create_pricing_bracket(min_students=1, max_students=100, fixed_fee_minor=9_999_999, effective_from=timezone.now())

        historical_result = fixed_fee_for_school(school, billable_student_count=50, as_of=before_change)
        current_result = fixed_fee_for_school(school, billable_student_count=50, as_of=timezone.now())
        self.assertEqual(historical_result.fixed_fee_minor, 4_000_000)
        self.assertEqual(current_result.fixed_fee_minor, 9_999_999)

    def test_a_new_bracket_overlapping_an_existing_ones_range_and_time_is_refused(self):
        create_pricing_bracket(min_students=1, max_students=100, fixed_fee_minor=4_000_000, effective_from=timezone.now() - timedelta(days=1))
        with self.assertRaises(OverlappingBracketError):
            create_pricing_bracket(min_students=50, max_students=150, fixed_fee_minor=5_000_000, effective_from=timezone.now() - timedelta(days=1))

    def test_a_replacement_bracket_for_the_same_range_is_allowed_once_the_old_one_is_closed_out(self):
        old = create_pricing_bracket(min_students=1, max_students=100, fixed_fee_minor=4_000_000, effective_from=timezone.now() - timedelta(days=30))
        cutover = timezone.now()
        old.effective_until = cutover
        old.save(update_fields=["effective_until"])
        create_pricing_bracket(min_students=1, max_students=100, fixed_fee_minor=4_500_000, effective_from=cutover)  # does not raise

    def test_two_brackets_covering_disjoint_ranges_at_the_same_time_are_fine(self):
        create_pricing_bracket(min_students=1, max_students=100, fixed_fee_minor=4_000_000, effective_from=timezone.now() - timedelta(days=1))
        create_pricing_bracket(min_students=101, max_students=300, fixed_fee_minor=7_500_000, effective_from=timezone.now() - timedelta(days=1))  # does not raise

    def test_an_open_ended_bracket_overlaps_every_higher_bracket_at_the_same_time(self):
        create_pricing_bracket(min_students=1, max_students=100, fixed_fee_minor=4_000_000, effective_from=timezone.now() - timedelta(days=1))
        with self.assertRaises(OverlappingBracketError):
            create_pricing_bracket(min_students=50, max_students=None, fixed_fee_minor=1, effective_from=timezone.now() - timedelta(days=1))

    def test_invalid_bracket_definitions_are_refused(self):
        with self.assertRaises(ValueError):
            create_pricing_bracket(min_students=0, max_students=100, fixed_fee_minor=1, effective_from=timezone.now())
        with self.assertRaises(ValueError):
            create_pricing_bracket(min_students=100, max_students=50, fixed_fee_minor=1, effective_from=timezone.now())
        with self.assertRaises(ValueError):
            create_pricing_bracket(min_students=1, max_students=100, fixed_fee_minor=-1, effective_from=timezone.now())
