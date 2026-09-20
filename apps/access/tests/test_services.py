from datetime import timedelta

from django.utils import timezone

from apps.access import catalog, services
from apps.access.models import AccessChange, MembershipActivity
from apps.access.services import AccessError

from .helpers import AccessTestCase

GRANT, BLOCK = "grant", "block"


class EffectiveAccessTests(AccessTestCase):
    def test_everyone_starts_on_their_roles_defaults(self):
        for role, member in self.members.items():
            self.assertEqual(services.effective_activities(member), catalog.default_keys(role), role)

    def test_a_block_takes_an_activity_away_from_one_person_only(self):
        second_teacher = self.other_teacher  # different school: must never be affected
        services.set_person_override(self.owner, self.members["teacher"].id, "teacher.cbt", BLOCK, mode="immediate")
        self.assertNotIn("teacher.cbt", services.effective_activities(self.members["teacher"]))
        self.assertIn("teacher.cbt", services.effective_activities(second_teacher))
        self.assertIn("teacher.timetable", services.effective_activities(self.members["teacher"]))

    def test_a_grant_gives_someone_an_activity_their_role_does_not_have(self):
        self.assertNotIn("finance.payroll", services.effective_activities(self.members["principal"]))
        services.set_person_override(self.owner, self.members["principal"].id, "finance.payroll", GRANT)
        self.assertIn("finance.payroll", services.effective_activities(self.members["principal"]))
        self.assertTrue(services.has_activity(self.members["principal"], "finance.payroll"))
        self.assertFalse(services.has_activity(self.members["teacher"], "finance.payroll"))

    def test_the_owner_always_has_everything_an_owner_has(self):
        self.assertEqual(services.effective_activities(self.owner), catalog.default_keys("proprietor"))

    def test_clearing_puts_the_person_back_on_their_roles_default(self):
        principal = self.members["principal"]
        services.set_person_override(self.owner, principal.id, "principal.ai", BLOCK, mode="immediate")
        services.set_person_override(self.owner, principal.id, "finance.payroll", GRANT)
        self.assertTrue(services.clear_person_override(self.owner, principal.id, "principal.ai"))
        self.assertTrue(services.clear_person_override(self.owner, principal.id, "finance.payroll"))
        self.assertEqual(services.effective_activities(principal), catalog.default_keys("principal"))
        self.assertFalse(services.clear_person_override(self.owner, principal.id, "principal.ai"))

    def test_changing_a_decision_replaces_it(self):
        principal = self.members["principal"]
        services.set_person_override(self.owner, principal.id, "principal.ai", BLOCK, mode="immediate")
        services.set_person_override(self.owner, principal.id, "principal.ai", GRANT)
        self.assertEqual(MembershipActivity.objects.filter(membership=principal).count(), 1)
        self.assertIn("principal.ai", services.effective_activities(principal))

    def test_a_decision_can_end_on_a_date(self):
        teacher = self.members["teacher"]
        soon = timezone.now() + timedelta(days=7)
        services.set_person_override(self.owner, teacher.id, "finance.payroll", GRANT, expires_at=soon)
        self.assertIn("finance.payroll", services.effective_activities(teacher))
        later = soon + timedelta(seconds=1)
        self.assertNotIn("finance.payroll", services.effective_activities(teacher, now=later))
        # An expired block gives the activity back.
        services.set_person_override(self.owner, teacher.id, "teacher.cbt", BLOCK, mode="immediate", expires_at=soon)
        self.assertNotIn("teacher.cbt", services.effective_activities(teacher))
        self.assertIn("teacher.cbt", services.effective_activities(teacher, now=later))

    def test_an_end_date_in_the_past_is_refused(self):
        with self.assertRaises(AccessError):
            services.set_person_override(
                self.owner, self.members["teacher"].id, "teacher.cbt", BLOCK, mode="immediate",
                expires_at=timezone.now() - timedelta(minutes=1),
            )


class OverrideRulesTests(AccessTestCase):
    def refused(self, *args, **kwargs):
        before = MembershipActivity.objects.count(), AccessChange.objects.count()
        with self.assertRaises(AccessError) as caught:
            services.set_person_override(*args, **kwargs)
        self.assertEqual((MembershipActivity.objects.count(), AccessChange.objects.count()), before)
        return caught.exception.message

    def test_nobody_but_the_owner_can_change_access(self):
        for role, member in self.members.items():
            if role != "proprietor":
                with self.assertRaises(AccessError, msg=role):
                    services.set_person_override(member, self.members["teacher"].id, "teacher.cbt", BLOCK, mode="immediate")
        # Not even the owner of a different school.
        with self.assertRaises(AccessError):
            services.set_person_override(self.other_owner, self.members["teacher"].id, "teacher.cbt", BLOCK, mode="immediate")

    def test_the_owner_cannot_be_blocked_or_changed(self):
        self.assertIn("owner", self.refused(self.owner, self.owner.id, "owner.payroll", BLOCK))
        with self.assertRaises(AccessError):
            services.clear_person_override(self.owner, self.owner.id, "owner.payroll")

    def test_a_landing_screen_cannot_be_blocked_for_the_role_that_needs_it(self):
        self.assertIn("landing screen", self.refused(self.owner, self.members["teacher"].id, "teacher.dashboard", BLOCK))
        self.assertIn("landing screen", self.refused(self.owner, self.members["parent"].id, "parent.dashboard", BLOCK))
        # Blocking another workspace's landing screen is harmless and allowed.
        services.set_person_override(self.owner, self.members["teacher"].id, "principal.dashboard", BLOCK, mode="immediate")

    def test_the_screen_that_manages_access_can_never_be_granted(self):
        self.assertIn("owner", self.refused(self.owner, self.members["principal"].id, "owner.access", GRANT))
        # ...but everything else owner-side can be, deliberately.
        services.set_person_override(self.owner, self.members["principal"].id, "owner.payroll", GRANT)

    def test_unknown_activities_effects_and_people_are_refused(self):
        teacher = self.members["teacher"].id
        self.refused(self.owner, teacher, "teacher.nonsense", BLOCK)
        self.refused(self.owner, teacher, "", BLOCK)
        self.refused(self.owner, teacher, "teacher.cbt", "maybe")
        self.refused(self.owner, "11111111-1111-1111-1111-111111111111", "teacher.cbt", BLOCK)
        self.refused(self.owner, self.other_teacher.id, "teacher.cbt", BLOCK)  # another school's person

    def test_an_inactive_member_cannot_be_targeted(self):
        teacher = self.members["teacher"]
        teacher.is_active = False
        teacher.save()
        self.refused(self.owner, teacher.id, "teacher.cbt", BLOCK)

    def test_notes_are_limited(self):
        self.refused(self.owner, self.members["teacher"].id, "teacher.cbt", BLOCK, note="x" * 201)


class ReassignTests(AccessTestCase):
    def test_an_activity_moves_from_one_person_to_another(self):
        principal, admin = self.members["principal"], self.members["administrator"]
        services.reassign(self.owner, "principal.approvals", principal.id, admin.id, note="Covering leave", mode="immediate")
        self.assertNotIn("principal.approvals", services.effective_activities(principal))
        self.assertIn("principal.approvals", services.effective_activities(admin))

    def test_it_is_all_or_nothing(self):
        principal, teacher = self.members["principal"], self.members["teacher"]
        # The person giving it up must have it.
        with self.assertRaises(AccessError):
            services.reassign(self.owner, "principal.approvals", teacher.id, principal.id)
        second = self.members["administrator"]
        services.set_person_override(self.owner, second.id, "principal.approvals", GRANT)
        before = MembershipActivity.objects.count()
        with self.assertRaises(AccessError):
            services.reassign(self.owner, "principal.approvals", principal.id, second.id)
        self.assertEqual(MembershipActivity.objects.count(), before)
        self.assertIn("principal.approvals", services.effective_activities(principal))

    def test_it_cannot_take_a_landing_screen_or_touch_the_owner(self):
        principal, admin = self.members["principal"], self.members["administrator"]
        with self.assertRaises(AccessError):
            services.reassign(self.owner, "principal.dashboard", principal.id, admin.id)
        with self.assertRaises(AccessError):
            services.reassign(self.owner, "owner.payroll", self.owner.id, principal.id)
        with self.assertRaises(AccessError):
            services.reassign(self.owner, "principal.approvals", principal.id, principal.id)
        self.assertIn("principal.dashboard", services.effective_activities(principal))

    def test_a_refused_reassign_changes_nothing(self):
        principal, admin = self.members["principal"], self.members["administrator"]
        # owner.access can never be held by anyone but the owner, so this is refused.
        # Whatever the reason, a refusal must leave every existing decision as it was.
        services.set_person_override(self.owner, principal.id, "owner.payroll", GRANT)
        before = list(MembershipActivity.objects.values_list("membership_id", "activity", "effect"))
        with self.assertRaises(AccessError):
            services.reassign(self.owner, "owner.access", principal.id, admin.id)
        self.assertEqual(before, list(MembershipActivity.objects.values_list("membership_id", "activity", "effect")))


class RoleDefaultsTests(AccessTestCase):
    def test_the_owner_changes_what_a_role_gets_for_everyone_in_it(self):
        keys = catalog.default_keys("teacher") - {"teacher.cbt"} | {"finance.receipts"}
        result = services.set_role_defaults(self.owner, "teacher", keys)
        self.assertEqual(result, keys)
        self.assertEqual(services.effective_activities(self.members["teacher"]), keys)
        # Other roles and other schools are untouched.
        self.assertEqual(services.effective_activities(self.members["principal"]), catalog.default_keys("principal"))
        self.assertEqual(services.effective_activities(self.other_teacher), catalog.default_keys("teacher"))

    def test_only_differences_from_the_built_in_defaults_are_stored(self):
        services.set_role_defaults(self.owner, "teacher", catalog.default_keys("teacher"))
        self.assertEqual(self.school.role_activities.count(), 0)
        services.set_role_defaults(self.owner, "teacher", catalog.default_keys("teacher") - {"teacher.cbt"})
        self.assertEqual(self.school.role_activities.count(), 1)

    def test_a_persons_own_decision_survives_a_change_to_their_role(self):
        teacher = self.members["teacher"]
        services.set_person_override(self.owner, teacher.id, "teacher.cbt", BLOCK, mode="immediate")
        services.set_role_defaults(self.owner, "teacher", catalog.default_keys("teacher") | {"finance.receipts"})
        self.assertNotIn("teacher.cbt", services.effective_activities(teacher))
        self.assertIn("finance.receipts", services.effective_activities(teacher))

    def test_landing_screens_and_owner_only_screens_are_protected(self):
        teacher = catalog.default_keys("teacher")
        for keys, fragment in [
            (teacher - {"teacher.dashboard"}, "landing screen"),
            (teacher | {"owner.access"}, "owner"),
            (teacher | {"nonsense.key"}, "not an activity"),
        ]:
            with self.assertRaises(AccessError, msg=fragment) as caught:
                services.set_role_defaults(self.owner, "teacher", keys)
            self.assertIn(fragment, caught.exception.message)
        self.assertEqual(self.school.role_activities.count(), 0)

    def test_the_owners_own_role_and_unknown_roles_cannot_be_edited(self):
        for role in ["proprietor", "emperor"]:
            with self.assertRaises(AccessError):
                services.set_role_defaults(self.owner, role, set())
            with self.assertRaises(AccessError):
                services.reset_role_defaults(self.owner, role)

    def test_only_the_owner_can_change_a_role(self):
        with self.assertRaises(AccessError):
            services.set_role_defaults(self.members["principal"], "teacher", catalog.default_keys("teacher"))
        with self.assertRaises(AccessError):
            services.reset_role_defaults(self.members["administrator"], "teacher")

    def test_resetting_goes_back_to_the_built_in_defaults(self):
        services.set_role_defaults(self.owner, "teacher", catalog.default_keys("teacher") - {"teacher.cbt"})
        result = services.reset_role_defaults(self.owner, "teacher")
        self.assertEqual(result, catalog.default_keys("teacher"))
        self.assertEqual(self.school.role_activities.count(), 0)


class AuditTests(AccessTestCase):
    def test_every_change_is_recorded_with_who_and_for_whom(self):
        teacher = self.members["teacher"]
        services.set_person_override(self.owner, teacher.id, "teacher.cbt", BLOCK, mode="immediate", note="Exam week")
        services.set_person_override(self.owner, teacher.id, "finance.payroll", GRANT)
        services.clear_person_override(self.owner, teacher.id, "teacher.cbt")
        services.set_role_defaults(self.owner, "parent", catalog.default_keys("parent") - {"parent.ai"})
        services.reset_role_defaults(self.owner, "parent")
        kinds = list(AccessChange.objects.order_by("id").values_list("kind", flat=True))
        self.assertEqual(kinds, ["person_block", "person_grant", "person_clear", "role_set", "role_reset"])
        first = AccessChange.objects.order_by("id").first()
        self.assertEqual((first.actor, first.target, first.activity), (self.owner, teacher, "teacher.cbt"))
        self.assertEqual(first.detail["note"], "Exam week")
        role_set = AccessChange.objects.get(kind="role_set")
        self.assertEqual((role_set.role, role_set.detail["removed"]), ("parent", ["parent.ai"]))

    def test_a_refused_change_leaves_no_trace(self):
        with self.assertRaises(AccessError):
            services.set_person_override(self.owner, self.members["teacher"].id, "teacher.dashboard", BLOCK, mode="immediate")
        self.assertEqual(AccessChange.objects.count(), 0)
