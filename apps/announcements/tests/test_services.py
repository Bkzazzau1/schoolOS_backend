"""Platform-operator announcements: creation validation, time-windowed activation (the
same effective_from/effective_until pattern as PricingBracket/EnterpriseAgreement/
SchoolDiscount), and targeting - a platform-wide announcement (organization=None) reaches
every organization, an organization-targeted one reaches only its own."""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from apps.billing.tests.base import verified_user
from apps.organizations.services import create_organization

from ..services import active_announcements_for_organization, create_announcement, end_announcement


def _organization(name="BrightGate Group"):
    owner = verified_user(f"owner-{name.lower().replace(' ', '-')}@example.ng")
    organization, _ = create_organization(actor=owner, name=name)
    return organization


class CreateAnnouncementValidationTests(TestCase):
    def test_a_title_is_required(self):
        with self.assertRaises(ValueError):
            create_announcement(title="  ", body="Save 10% on annual plans.")

    def test_a_body_is_required(self):
        with self.assertRaises(ValueError):
            create_announcement(title="Annual discount", body="   ")

    def test_effective_until_must_be_after_effective_from(self):
        now = timezone.now()
        with self.assertRaises(ValueError):
            create_announcement(
                title="Annual discount", body="Save 10%.",
                effective_from=now, effective_until=now - timedelta(days=1),
            )


class ActiveAnnouncementsForOrganizationTests(TestCase):
    def test_a_platform_wide_announcement_reaches_every_organization(self):
        org_a = _organization("Org A")
        org_b = _organization("Org B")
        create_announcement(title="Platform notice", body="Scheduled maintenance Friday.")

        self.assertEqual(active_announcements_for_organization(org_a).count(), 1)
        self.assertEqual(active_announcements_for_organization(org_b).count(), 1)

    def test_an_organization_targeted_announcement_reaches_only_its_own(self):
        org_a = _organization("Org A")
        org_b = _organization("Org B")
        create_announcement(title="Just for you", body="Your renewal discount.", organization=org_a)

        self.assertEqual(active_announcements_for_organization(org_a).count(), 1)
        self.assertEqual(active_announcements_for_organization(org_b).count(), 0)

    def test_an_ended_announcement_is_no_longer_active(self):
        org = _organization()
        announcement = create_announcement(title="Flash sale", body="Today only.")
        end_announcement(announcement)
        self.assertEqual(active_announcements_for_organization(org).count(), 0)

    def test_an_announcement_that_has_not_started_yet_is_not_active(self):
        org = _organization()
        create_announcement(
            title="Coming soon", body="Next week's news.",
            effective_from=timezone.now() + timedelta(days=7),
        )
        self.assertEqual(active_announcements_for_organization(org).count(), 0)

    def test_ending_an_already_ended_announcement_is_refused(self):
        announcement = create_announcement(title="Flash sale", body="Today only.")
        end_announcement(announcement)
        with self.assertRaises(ValueError):
            end_announcement(announcement)
