"""_principal_secondary_scope (apps.academics.curriculum_services): a Principal may
manage only Secondary curriculum/teaching assignments, and - the newer rule - anyone
the owner made sectionHead of a Nursery/Primary/... section (Job Assignments, the same
screen that already scopes someone to one campus) is limited to that section's own
stage the same way, with no change for anyone the owner never scoped."""

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.academics.curriculum_services import _principal_secondary_scope
from apps.academics.models import AcademicClass
from apps.core.errors import Rejected
from apps.schools.models import Membership, Role, School
from apps.sync.models import SyncRecord

User = get_user_model()
PASSWORD = "a-long-test-password-1"


class PrincipalAndStageScopeTests(TestCase):
    def setUp(self):
        self.school = School.objects.create(name="BrightGate", slug="brightgate-stage-scope")
        self.secondary_class = AcademicClass.objects.create(
            school=self.school, code="JSS2A", name="JSS 2A", section="Secondary", level_order=1,
        )
        self.primary_class = AcademicClass.objects.create(
            school=self.school, code="P5A", name="Primary 5A", section="Primary", level_order=2,
        )
        self.nursery_class = AcademicClass.objects.create(
            school=self.school, code="N1A", name="Nursery 1A", section="Nursery", level_order=3,
        )

    def _member(self, role, email):
        return Membership.objects.create(user=User.objects.create_user(email, PASSWORD), school=self.school, role=role)

    def _assign_section_head(self, membership, *, stage, job_id="J1"):
        SyncRecord.objects.create(
            school=self.school, entity_type="academic_section", entity_id=f"SEC-{job_id}",
            payload={"id": f"SEC-{job_id}", "stage": stage},
        )
        SyncRecord.objects.create(
            school=self.school, entity_type="owner_job_assignment", entity_id=job_id,
            payload={"status": "active", "membershipId": str(membership.id), "role": "sectionHead", "sectionId": f"SEC-{job_id}"},
        )

    def test_a_principal_may_manage_secondary(self):
        principal = self._member(Role.PRINCIPAL, "principal@brightgate.ng")
        _principal_secondary_scope(principal, self.secondary_class)  # does not raise

    def test_a_principal_may_not_manage_primary_or_nursery(self):
        principal = self._member(Role.PRINCIPAL, "principal2@brightgate.ng")
        with self.assertRaises(Rejected):
            _principal_secondary_scope(principal, self.primary_class)
        with self.assertRaises(Rejected):
            _principal_secondary_scope(principal, self.nursery_class)

    def test_an_ordinary_teacher_with_no_assignment_is_never_restricted(self):
        teacher = self._member(Role.TEACHER, "teacher@brightgate.ng")
        for academic_class in (self.secondary_class, self.primary_class, self.nursery_class):
            _principal_secondary_scope(teacher, academic_class)  # does not raise

    def test_a_teacher_made_sectionhead_of_nursery_may_manage_only_nursery(self):
        teacher = self._member(Role.TEACHER, "headteacher@brightgate.ng")
        self._assign_section_head(teacher, stage="Nursery")
        _principal_secondary_scope(teacher, self.nursery_class)  # does not raise
        with self.assertRaises(Rejected):
            _principal_secondary_scope(teacher, self.primary_class)
        with self.assertRaises(Rejected):
            _principal_secondary_scope(teacher, self.secondary_class)

    def test_an_administrator_made_sectionhead_of_primary_may_manage_only_primary(self):
        """Not only a Teacher - whoever the owner names sectionHead, regardless of
        their underlying staff role, gets the same real scoping."""
        admin = self._member(Role.ADMINISTRATOR, "headmaster@brightgate.ng")
        self._assign_section_head(admin, stage="Primary")
        _principal_secondary_scope(admin, self.primary_class)  # does not raise
        with self.assertRaises(Rejected):
            _principal_secondary_scope(admin, self.nursery_class)

    def test_a_pending_sectionhead_assignment_does_not_scope_yet(self):
        teacher = self._member(Role.TEACHER, "teacher3@brightgate.ng")
        SyncRecord.objects.create(
            school=self.school, entity_type="academic_section", entity_id="SEC-J1",
            payload={"id": "SEC-J1", "stage": "Nursery"},
        )
        SyncRecord.objects.create(
            school=self.school, entity_type="owner_job_assignment", entity_id="J1",
            payload={"status": "pendingActivation", "membershipId": str(teacher.id), "role": "sectionHead", "sectionId": "SEC-J1"},
        )
        for academic_class in (self.secondary_class, self.primary_class, self.nursery_class):
            _principal_secondary_scope(teacher, academic_class)  # not active yet: whole-school still
