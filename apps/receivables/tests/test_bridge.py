from io import StringIO

from django.core.management import CommandError, call_command

from .. import bridge, families
from ..models import Family, FamilyGuardian, FamilyStudent, FinanceAuditEvent
from .base import ReceivablesTestCase

SENTINEL = "Create new family account"


class BridgeTests(ReceivablesTestCase):
    def test_students_sharing_exactly_the_same_explicit_reference_become_one_family(self):
        a = self.make_student("Ahmad", "Bello", guardian="Musa Bello", ref="Bello Family Account")
        b = self.make_student("Aisha", "Bello", guardian="Musa Bello", ref="Bello Family Account")
        c = self.make_student("Yusuf", "Sani", guardian="Ada Sani", ref="Sani Family Account")
        report = bridge.apply(self.school)
        self.assertEqual((report.families_created, report.students_linked), (2, 3))
        self.assertEqual(families.family_of(a), families.family_of(b))
        self.assertNotEqual(families.family_of(a), families.family_of(c))
        self.assertEqual(families.family_of(a).display_name, "Bello Family Account")

    def test_names_alone_never_join_students(self):
        a = self.make_student("Ahmad", "Bello", guardian="Musa Bello", ref=SENTINEL)
        b = self.make_student("Aisha", "Bello", guardian="Musa Bello", ref=SENTINEL)
        report = bridge.apply(self.school)
        self.assertEqual((report.families_created, report.students_linked), (0, 0))
        self.assertIsNone(families.family_of(a))
        self.assertIsNone(families.family_of(b))

    def test_the_new_account_label_and_blanks_are_not_references(self):
        for label in (SENTINEL, "create new family account", "  ", "", "None", "n/a", "-"):
            self.make_student("A", "B", guardian="G", ref=label)
        report = bridge.analyse(self.school)
        self.assertEqual((len(report.groups), len(report.without_reference)), (0, 7))

    def test_a_reference_that_differs_only_in_case_is_not_assumed_to_be_the_same_family(self):
        a = self.make_student("Ahmad", "Bello", guardian="Musa", ref="Bello Family")
        b = self.make_student("Aisha", "Bello", guardian="Musa", ref="bello family")
        bridge.apply(self.school)
        self.assertNotEqual(families.family_of(a), families.family_of(b))

    def test_a_student_whose_records_name_two_different_references_is_left_alone_and_reported(self):
        a = self.make_student("Ahmad", "Bello", guardian="Musa", ref="Ref One", with_registration=True)
        a.registration.family_account_ref = "Ref Two"
        a.registration.save()
        report = bridge.apply(self.school)
        self.assertEqual(len(report.conflicts), 1)
        self.assertEqual(report.conflicts[0][1], ["Ref One", "Ref Two"])
        self.assertIsNone(families.family_of(a))

    def test_the_registration_and_the_guardian_agreeing_is_not_a_conflict(self):
        a = self.make_student("Ahmad", "Bello", guardian="Musa", ref="Bello Family", with_registration=True)
        report = bridge.apply(self.school)
        self.assertEqual((len(report.conflicts), report.students_linked), (0, 1))
        self.assertIsNotNone(families.family_of(a))

    def test_a_reference_only_on_the_registration_is_used_too(self):
        a = self.make_student("Ahmad", "Bello", with_registration=True, ref="Bello Family")
        bridge.apply(self.school)
        self.assertIsNotNone(families.family_of(a))

    def test_students_who_have_left_or_are_already_in_a_family_are_left_alone(self):
        left = self.make_student("Old", "Bello", guardian="Musa", ref="Bello Family", status="graduated")
        placed = self.make_student("Ahmad", "Bello", guardian="Musa", ref="Bello Family")
        mine = families.create_family(self.school, display_name="Chosen by a person", students=[placed])
        newcomer = self.make_student("Aisha", "Bello", guardian="Musa", ref="Bello Family")
        report = bridge.apply(self.school)
        self.assertEqual(report.already_in_family, 1)
        self.assertIsNone(families.family_of(left))
        self.assertEqual(families.family_of(placed), mine)  # never moved
        self.assertNotEqual(families.family_of(newcomer), mine)  # and never merged into it on a guess

    def test_running_it_again_finds_the_family_it_made_and_adds_to_it(self):
        a = self.make_student("Ahmad", "Bello", guardian="Musa", ref="Bello Family")
        bridge.apply(self.school)
        b = self.make_student("Aisha", "Bello", guardian="Musa", ref="Bello Family")
        report = bridge.apply(self.school)
        self.assertEqual((report.families_created, report.students_linked), (0, 1))
        self.assertEqual(families.family_of(a), families.family_of(b))
        self.assertEqual(Family.objects.filter(school=self.school).count(), 1)
        again = bridge.apply(self.school)
        self.assertEqual((again.families_created, again.students_linked), (0, 0))

    def test_a_family_a_person_closed_is_not_reopened_by_the_bridge(self):
        bridge.apply(self.school)  # nothing yet
        a = self.make_student("Ahmad", "Bello", guardian="Musa", ref="Bello Family")
        bridge.apply(self.school)
        family = families.family_of(a)
        families.remove_student(family, a)
        families.set_status(family, "inactive")
        bridge.apply(self.school)
        self.assertIsNone(families.family_of(a))

    def test_singletons_give_everyone_else_a_family_of_their_own_and_nothing_more(self):
        a = self.make_student("Ahmad", "Bello", guardian="Musa Bello", ref=SENTINEL)
        b = self.make_student("Aisha", "Bello", guardian="Musa Bello", ref=SENTINEL)
        report = bridge.apply(self.school, singletons=True)
        self.assertEqual((report.families_created, report.students_linked), (2, 2))
        self.assertNotEqual(families.family_of(a), families.family_of(b))
        self.assertEqual(FamilyGuardian.objects.filter(family=families.family_of(a)).count(), 1)

    def test_a_sibling_hint_is_reported_for_a_person_but_acted_on_by_nobody(self):
        a = self.make_student("Ahmad", "Bello", guardian="Musa", ref=SENTINEL, sibling="Aisha Bello (JSS 1)")
        report = bridge.analyse(self.school)
        self.assertEqual(report.sibling_hints, [a])
        bridge.apply(self.school)
        self.assertIsNone(families.family_of(a))

    def test_the_guardians_come_across_as_payers(self):
        a = self.make_student("Ahmad", "Bello", guardian="Musa Bello", phone="08031111111", ref="Bello Family")
        bridge.apply(self.school)
        payer = families.primary_payer(families.family_of(a))
        self.assertEqual((payer.guardian.name, payer.guardian.phone), ("Musa Bello", "08031111111"))

    def test_it_only_touches_the_school_it_is_asked_about(self):
        mine = self.make_student("Ahmad", "Bello", guardian="Musa", ref="Bello Family")
        theirs = self.make_student("Ahmad", "Bello", guardian="Musa", ref="Bello Family", school=self.other_school)
        bridge.apply(self.school)
        self.assertIsNotNone(families.family_of(mine))
        self.assertIsNone(families.family_of(theirs))
        bridge.apply(self.other_school)
        self.assertNotEqual(families.family_of(mine), families.family_of(theirs))
        self.assertEqual(families.family_of(theirs).school, self.other_school)

    def test_the_analysis_changes_nothing(self):
        self.make_student("Ahmad", "Bello", guardian="Musa", ref="Bello Family")
        report = bridge.analyse(self.school)
        self.assertEqual(len(report.groups), 1)
        self.assertEqual((Family.objects.count(), FamilyStudent.objects.count()), (0, 0))

    def test_the_run_is_audited(self):
        self.make_student("Ahmad", "Bello", guardian="Musa", ref="Bello Family")
        bridge.apply(self.school)
        event = FinanceAuditEvent.objects.get(kind="families_bridged")
        self.assertEqual((event.detail["created"], event.detail["linked"]), (1, 1))


class BridgeCommandTests(ReceivablesTestCase):
    def run_command(self, *args):
        out = StringIO()
        call_command("bridge_families", *args, stdout=out)
        return out.getvalue()

    def test_without_apply_it_only_reports(self):
        self.make_student("Ahmad", "Bello", guardian="Musa", ref="Bello Family")
        output = self.run_command("--school", self.school.slug)
        self.assertIn("would make a family", output)
        self.assertIn("Report only", output)
        self.assertEqual(Family.objects.count(), 0)

    def test_with_apply_it_makes_the_families(self):
        self.make_student("Ahmad", "Bello", guardian="Musa", ref="Bello Family")
        self.make_student("Aisha", "Sani", guardian="Ada", ref=SENTINEL)
        output = self.run_command("--school", self.school.slug, "--apply", "--singletons")
        self.assertIn("made 2 family(ies), linked 2 student(s)", output)
        self.assertEqual(Family.objects.count(), 2)

    def test_an_unknown_school_is_refused(self):
        with self.assertRaises(CommandError):
            self.run_command("--school", "nowhere")
