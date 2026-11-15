from apps.owner.jobs.access import campus_scope_for, has_duty, stage_scope_for
from apps.sync.models import SyncRecord

from .helpers import OwnerTestCase


class HasDutyTests(OwnerTestCase):
    def test_an_active_assignment_linked_to_this_membership_grants_its_duties(self):
        teacher = self.members["teacher"]
        SyncRecord.objects.create(
            school=self.school, entity_type="owner_job_assignment", entity_id="J1",
            payload={"status": "active", "membershipId": str(teacher.id), "duties": ["finance.fees"]},
        )
        self.assertTrue(has_duty(teacher, "finance.fees"))
        self.assertFalse(has_duty(teacher, "finance.payroll"))

    def test_a_pending_or_revoked_assignment_grants_nothing(self):
        teacher = self.members["teacher"]
        SyncRecord.objects.create(
            school=self.school, entity_type="owner_job_assignment", entity_id="J1",
            payload={"status": "pendingActivation", "membershipId": str(teacher.id), "duties": ["finance.fees"]},
        )
        self.assertFalse(has_duty(teacher, "finance.fees"))

    def test_someone_elses_assignment_grants_nothing(self):
        teacher, staff = self.members["teacher"], self.members["staff"]
        SyncRecord.objects.create(
            school=self.school, entity_type="owner_job_assignment", entity_id="J1",
            payload={"status": "active", "membershipId": str(staff.id), "duties": ["finance.fees"]},
        )
        self.assertFalse(has_duty(teacher, "finance.fees"))


class CampusScopeForTests(OwnerTestCase):
    def test_no_assignment_at_all_means_whole_school(self):
        self.assertIsNone(campus_scope_for(self.members["administrator"]))

    def test_a_direct_campus_grant_scopes_to_that_campus(self):
        admin = self.members["administrator"]
        SyncRecord.objects.create(
            school=self.school, entity_type="owner_job_assignment", entity_id="J1",
            payload={"status": "active", "membershipId": str(admin.id), "duties": ["administration.students"], "campusId": "CAMPUS-1"},
        )
        self.assertEqual(campus_scope_for(admin), "CAMPUS-1")

    def test_a_section_head_grant_resolves_through_the_sections_own_real_campus(self):
        principal = self.members["principal"]
        SyncRecord.objects.create(
            school=self.school, entity_type="academic_section", entity_id="SEC-1",
            payload={"id": "SEC-1", "campusId": "CAMPUS-2"},
        )
        SyncRecord.objects.create(
            school=self.school, entity_type="owner_job_assignment", entity_id="J1",
            payload={"status": "active", "membershipId": str(principal.id), "duties": ["academics.teaching"], "sectionId": "SEC-1"},
        )
        self.assertEqual(campus_scope_for(principal), "CAMPUS-2")

    def test_a_section_head_grant_for_a_section_with_no_campus_yet_stays_whole_school(self):
        principal = self.members["principal"]
        SyncRecord.objects.create(
            school=self.school, entity_type="academic_section", entity_id="SEC-1",
            payload={"id": "SEC-1", "campusId": None},
        )
        SyncRecord.objects.create(
            school=self.school, entity_type="owner_job_assignment", entity_id="J1",
            payload={"status": "active", "membershipId": str(principal.id), "duties": ["academics.teaching"], "sectionId": "SEC-1"},
        )
        self.assertIsNone(campus_scope_for(principal))

    def test_an_inactive_or_someone_elses_assignment_never_scopes_this_membership(self):
        admin, staff = self.members["administrator"], self.members["staff"]
        SyncRecord.objects.create(
            school=self.school, entity_type="owner_job_assignment", entity_id="J1",
            payload={"status": "pendingActivation", "membershipId": str(admin.id), "duties": [], "campusId": "CAMPUS-1"},
        )
        SyncRecord.objects.create(
            school=self.school, entity_type="owner_job_assignment", entity_id="J2",
            payload={"status": "active", "membershipId": str(staff.id), "duties": [], "campusId": "CAMPUS-1"},
        )
        self.assertIsNone(campus_scope_for(admin))

    def test_another_schools_assignment_never_scopes_this_membership(self):
        from apps.schools.models import School

        other_school = School.objects.create(name="Other", slug="other")
        admin = self.members["administrator"]
        SyncRecord.objects.create(
            school=other_school, entity_type="owner_job_assignment", entity_id="J1",
            payload={"status": "active", "membershipId": str(admin.id), "duties": [], "campusId": "CAMPUS-1"},
        )
        self.assertIsNone(campus_scope_for(admin))


class StageScopeForTests(OwnerTestCase):
    """Lets anyone - not only a Principal - be scoped to one academic stage (Nursery,
    Primary, ...) the owner assigns them sectionHead of, the same Job Assignments
    screen that already scopes someone to one campus."""

    def test_no_assignment_at_all_means_whole_school(self):
        self.assertIsNone(stage_scope_for(self.members["teacher"]))

    def test_a_sectionhead_grant_resolves_through_the_sections_own_real_stage(self):
        teacher = self.members["teacher"]
        SyncRecord.objects.create(
            school=self.school, entity_type="academic_section", entity_id="SEC-1",
            payload={"id": "SEC-1", "stage": "Nursery"},
        )
        SyncRecord.objects.create(
            school=self.school, entity_type="owner_job_assignment", entity_id="J1",
            payload={"status": "active", "membershipId": str(teacher.id), "role": "sectionHead", "sectionId": "SEC-1"},
        )
        self.assertEqual(stage_scope_for(teacher), "Nursery")

    def test_a_grant_with_a_different_job_role_never_scopes_by_stage(self):
        """sectionId alone is not enough - unlike campus_scope_for, a cleaner or driver
        tied to a section is not thereby restricted in curriculum access, which they
        never had any access to in the first place."""
        staff = self.members["staff"]
        SyncRecord.objects.create(
            school=self.school, entity_type="academic_section", entity_id="SEC-1",
            payload={"id": "SEC-1", "stage": "Nursery"},
        )
        SyncRecord.objects.create(
            school=self.school, entity_type="owner_job_assignment", entity_id="J1",
            payload={"status": "active", "membershipId": str(staff.id), "role": "cleaner", "sectionId": "SEC-1"},
        )
        self.assertIsNone(stage_scope_for(staff))

    def test_a_sectionhead_grant_for_a_section_with_no_stage_yet_stays_whole_school(self):
        teacher = self.members["teacher"]
        SyncRecord.objects.create(
            school=self.school, entity_type="academic_section", entity_id="SEC-1",
            payload={"id": "SEC-1", "stage": ""},
        )
        SyncRecord.objects.create(
            school=self.school, entity_type="owner_job_assignment", entity_id="J1",
            payload={"status": "active", "membershipId": str(teacher.id), "role": "sectionHead", "sectionId": "SEC-1"},
        )
        self.assertIsNone(stage_scope_for(teacher))

    def test_an_inactive_or_someone_elses_assignment_never_scopes_this_membership(self):
        teacher, staff = self.members["teacher"], self.members["staff"]
        SyncRecord.objects.create(
            school=self.school, entity_type="academic_section", entity_id="SEC-1",
            payload={"id": "SEC-1", "stage": "Nursery"},
        )
        SyncRecord.objects.create(
            school=self.school, entity_type="owner_job_assignment", entity_id="J1",
            payload={"status": "pendingActivation", "membershipId": str(teacher.id), "role": "sectionHead", "sectionId": "SEC-1"},
        )
        SyncRecord.objects.create(
            school=self.school, entity_type="owner_job_assignment", entity_id="J2",
            payload={"status": "active", "membershipId": str(staff.id), "role": "sectionHead", "sectionId": "SEC-1"},
        )
        self.assertIsNone(stage_scope_for(teacher))
