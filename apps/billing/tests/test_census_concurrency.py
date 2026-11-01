"""Phase D, Stage 4, Task 4/9 audit finding: correct_school_census() and
freeze_school_census() had no row-level locking, so two concurrent corrections of the same
census row (e.g. two operators both opening apps.console's census-exceptions page before
either submits) could both silently succeed, each creating its own "replacement" row with no
conflict ever raised. Fixed by locking the owning School row for the duration of the
decision and re-reading the census under that lock before acting - simulated here the same
way the original audit probe did: two independent in-memory copies of the same row, each
fetched before either call runs, exactly like two separate request handlers calling
apps.console.views.census_exceptions's get_object_or_404 would."""

from datetime import timedelta

from django.utils import timezone

from apps.billing.census import correct_school_census, flag_pricing_exception
from apps.billing.models import SchoolEnrollmentCensus

from .base import BillingTestCase


class ConcurrentCensusCorrectionTests(BillingTestCase):
    def test_a_second_correction_of_an_already_corrected_census_is_refused_not_silent(self):
        start = timezone.now() - timedelta(days=1)
        end = timezone.now() + timedelta(days=89)
        census = flag_pricing_exception(self.school, period_start=start, period_end=end, reason="Zero.")

        # Two handlers, each independently fetching the row fresh before either submits.
        copy_a = SchoolEnrollmentCensus.objects.get(pk=census.pk)
        copy_b = SchoolEnrollmentCensus.objects.get(pk=census.pk)

        result_a = correct_school_census(
            copy_a, actor=self.owner, reason="Operator A's count.", billable_student_count=50,
        )
        self.assertEqual(result_a.billable_student_count, 50)

        with self.assertRaises(ValueError):
            correct_school_census(
                copy_b, actor=self.owner, reason="Operator B's count.", billable_student_count=90,
            )

        # Exactly one replacement exists - operator B's call was refused, not silently
        # accepted alongside A's.
        self.assertEqual(SchoolEnrollmentCensus.objects.filter(corrected_from=census).count(), 1)
