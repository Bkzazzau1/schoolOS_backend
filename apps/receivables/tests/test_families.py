from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.students.models import GuardianLink

from .. import families
from ..errors import Refused
from ..models import Family, FamilyGuardian, FamilyStatus, FamilyStudent, FinanceAuditEvent
from .base import ReceivablesTestCase


class FamilyCreationTests(ReceivablesTestCase):
    def test_a_family_gets_a_code_and_its_students(self):
        ahmad, aisha = self.make_student("Ahmad", "Bello"), self.make_student("Aisha", "Bello")
        family = families.create_family(self.school, display_name="  Bello   family ", actor=self.owner, students=[ahmad, aisha])
        self.assertRegex(family.code, r"^FAM-[A-HJ-NP-Z2-9]{8}$")
        self.assertEqual((family.display_name, family.status, family.created_by), ("Bello family", "active", self.owner))
        self.assertEqual({s.id for s in families.active_students(family)}, {ahmad.id, aisha.id})
        self.assertEqual(families.family_of(ahmad), family)

    def test_codes_are_unique_at_a_school_and_can_repeat_across_schools(self):
        a = families.create_family(self.school, display_name="A")
        b = families.create_family(self.school, display_name="B")
        self.assertNotEqual(a.code, b.code)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Family.objects.create(school=self.school, code=a.code, display_name="dup")
        Family.objects.create(school=self.other_school, code=a.code, display_name="elsewhere")

    def test_a_family_needs_a_name(self):
        for name in ("", "   ", None):
            with self.assertRaises(Refused):
                families.create_family(self.school, display_name=name)
        with self.assertRaises(Refused):
            families.create_family(self.school, display_name="x" * 201)

    def test_a_code_and_a_school_never_change(self):
        family = families.create_family(self.school, display_name="A")
        family.code = "FAM-CHANGED1"
        with self.assertRaises(ValidationError):
            family.save()
        family.refresh_from_db()
        family.school = self.other_school
        with self.assertRaises(ValidationError):
            family.save()

    def test_creating_a_family_is_audited(self):
        family = families.create_family(self.school, display_name="Bello family", actor=self.owner)
        event = FinanceAuditEvent.objects.get(kind="family_created")
        self.assertEqual((event.actor, event.object_id, event.detail["code"]), (self.owner, str(family.id), family.code))

    def test_a_refused_creation_leaves_nothing_behind(self):
        taken = self.make_student("Ahmad", "Bello")
        families.create_family(self.school, display_name="First", students=[taken])
        fresh = self.make_student("Aisha", "Bello")
        with self.assertRaises(Refused):
            families.create_family(self.school, display_name="Second", students=[fresh, taken])
        self.assertFalse(Family.objects.filter(display_name="Second").exists())
        self.assertIsNone(families.family_of(fresh))


class StudentMembershipTests(ReceivablesTestCase):
    def setUp(self):
        super().setUp()
        self.ahmad, self.aisha = self.make_student("Ahmad", "Bello"), self.make_student("Aisha", "Bello")
        self.family = families.create_family(self.school, display_name="Bello family", students=[self.ahmad])

    def test_a_student_is_never_in_two_active_families(self):
        other = families.create_family(self.school, display_name="Other")
        with self.assertRaises(Refused) as raised:
            families.add_student(other, self.ahmad)
        self.assertEqual(raised.exception.code, "student_in_family")
        self.assertIn(self.family.code, raised.exception.message)
        with self.assertRaises(Refused) as again:
            families.add_student(self.family, self.ahmad)
        self.assertEqual(again.exception.code, "already_member")

    def test_the_database_holds_the_line_even_if_a_service_is_bypassed(self):
        other = families.create_family(self.school, display_name="Other")
        with self.assertRaises(IntegrityError), transaction.atomic():
            FamilyStudent.objects.create(family=other, student=self.ahmad)

    def test_a_student_can_be_added_and_removed_and_kept_in_the_history(self):
        families.add_student(self.family, self.aisha, actor=self.owner)
        families.remove_student(self.family, self.aisha, actor=self.owner)
        self.assertIsNone(families.family_of(self.aisha))
        row = FamilyStudent.objects.get(student=self.aisha)
        self.assertEqual((row.is_active, row.left_at is not None), (False, True))
        # ...and can then join another family.
        other = families.create_family(self.school, display_name="Other")
        families.add_student(other, self.aisha)
        self.assertEqual(families.family_of(self.aisha), other)
        self.assertEqual(FamilyStudent.objects.filter(student=self.aisha).count(), 2)

    def test_removing_someone_who_is_not_a_member_is_refused(self):
        with self.assertRaises(Refused):
            families.remove_student(self.family, self.aisha)

    def test_a_closed_family_takes_no_new_students(self):
        families.set_status(self.family, FamilyStatus.INACTIVE, actor=self.owner)
        with self.assertRaises(Refused) as raised:
            families.add_student(self.family, self.aisha)
        self.assertEqual(raised.exception.code, "family_closed")

    def test_a_student_from_another_school_can_never_be_linked(self):
        stranger = self.make_student("Zed", "Other", school=self.other_school)
        with self.assertRaises(Refused):
            families.add_student(self.family, stranger)
        with self.assertRaises(ValidationError):  # and the model itself refuses if a service is bypassed
            FamilyStudent(family=self.family, student=stranger).save()
        self.assertFalse(FamilyStudent.objects.filter(student=stranger).exists())

    def test_every_change_is_audited(self):
        families.add_student(self.family, self.aisha, actor=self.owner)
        families.remove_student(self.family, self.aisha, actor=self.owner)
        families.rename(self.family, "The Bellos", actor=self.owner)
        kinds = set(FinanceAuditEvent.objects.values_list("kind", flat=True))
        self.assertTrue({"family_created", "family_student_added", "family_student_removed", "family_renamed"} <= kinds)


class GuardianTests(ReceivablesTestCase):
    def setUp(self):
        super().setUp()
        self.ahmad = self.make_student("Ahmad", "Bello", guardian="Musa Bello", phone="08031111111")
        self.aisha = self.make_student("Aisha", "Bello", guardian="Musa Bello", phone="08031111111")
        self.family = families.create_family(self.school, display_name="Bello family", students=[self.ahmad, self.aisha])

    def test_the_payers_are_the_guardians_already_on_the_students(self):
        families.link_guardians_of(self.family, self.ahmad)
        families.link_guardians_of(self.family, self.aisha)
        payers = FamilyGuardian.objects.filter(family=self.family)
        self.assertEqual(payers.count(), 2)
        primary = families.primary_payer(self.family)
        self.assertEqual((primary.guardian.name, primary.guardian.phone), ("Musa Bello", "08031111111"))
        self.assertEqual(payers.filter(is_primary_payer=True).count(), 1)

    def test_a_payer_refers_to_the_guardian_it_does_not_copy_them(self):
        families.link_guardians_of(self.family, self.ahmad)
        link = FamilyGuardian.objects.get()
        GuardianLink.objects.filter(id=link.guardian_id).update(phone="08039999999")
        link.refresh_from_db()
        self.assertEqual(link.guardian.phone, "08039999999")

    def test_only_one_primary_payer_at_a_time(self):
        first = families.link_guardian(self.family, self.ahmad.guardians.get(), primary=True)
        second = families.link_guardian(self.family, self.aisha.guardians.get(), primary=True)
        first.refresh_from_db()
        self.assertEqual((first.is_primary_payer, second.is_primary_payer), (False, True))
        with self.assertRaises(IntegrityError), transaction.atomic():
            FamilyGuardian.objects.filter(pk=first.pk).update(is_primary_payer=True)

    def test_a_payer_must_be_a_guardian_of_someone_in_the_family(self):
        outsider = self.make_student("Yusuf", "Sani", guardian="Ada Sani")
        with self.assertRaises(Refused) as raised:
            families.link_guardian(self.family, outsider.guardians.get())
        self.assertEqual(raised.exception.code, "guardian_not_in_family")
        stranger = self.make_student("Zed", "Other", school=self.other_school, guardian="Zed Sr")
        with self.assertRaises(Refused):
            families.link_guardian(self.family, stranger.guardians.get())

    def test_a_guardian_stops_paying_when_their_only_child_leaves(self):
        loner = self.make_student("Bala", "Bello", guardian="Bala Sr", phone="08032222222")
        families.add_student(self.family, loner)
        families.link_guardians_of(self.family, loner)
        families.remove_student(self.family, loner)
        self.assertFalse(FamilyGuardian.objects.get(guardian__student=loner).is_active)


class EnsureFamilyTests(ReceivablesTestCase):
    def test_a_student_with_no_family_gets_one_of_their_own_and_is_never_merged(self):
        ahmad = self.make_student("Ahmad", "Bello", guardian="Musa Bello")
        aisha = self.make_student("Aisha", "Bello", guardian="Musa Bello")
        first, made = families.ensure_family_for_student(self.school, ahmad)
        second, made_again = families.ensure_family_for_student(self.school, aisha)
        self.assertEqual((made, made_again, first.display_name), (True, True, "Bello family"))
        self.assertNotEqual(first.id, second.id)  # same surname, same guardian: still not merged on a guess
        self.assertEqual(families.ensure_family_for_student(self.school, ahmad), (first, False))

    def test_search_finds_families_by_name_code_or_student(self):
        ahmad = self.make_student("Ahmad", "Bello")
        family = families.create_family(self.school, display_name="The Bellos", students=[ahmad])
        other = families.create_family(self.other_school, display_name="The Bellos elsewhere")
        for term in ("bellos", family.code[:8].lower(), "ahmad", ahmad.student_code, ahmad.admission_number):
            self.assertEqual([f.id for f in families.search(self.school, term)], [family.id], term)
        self.assertEqual(len(families.search(self.school, "")), 1)
        self.assertNotIn(other.id, [f.id for f in families.search(self.school, "")])
