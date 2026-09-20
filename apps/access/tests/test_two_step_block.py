from datetime import timedelta

from django.test import override_settings
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

from apps.access import services
from apps.access.models import AccessChange, MembershipActivity
from apps.access.permissions import require_activity

from .helpers import AccessTestCase

KEY = "teacher.cbt"


class TwoStepBlockTests(AccessTestCase):
    """The owner blocks; the app first fetches and submits; then it takes effect."""

    def setUp(self):
        super().setUp()
        self.teacher = self.members["teacher"]

    def block(self, mode=None, key=KEY, **extra):
        body = {"effect": "block", **extra}
        if mode:
            body["mode"] = mode
        return self.call("put", self.person_path(self.teacher, key), self.owner, body)

    def acknowledge(self, *keys, who=None):
        return self.call("post", f"schools/{self.school.id}/access/acknowledge/", who or self.teacher,
                         {"activities": list(keys)})

    # -- the default is to wait for the person's app --------------------------------

    def test_a_block_waits_for_the_persons_app_by_default(self):
        response = self.block()
        self.assertEqual(response.status_code, 200)
        me = self.me(self.teacher).json()
        # They still have it, and are told it is going.
        self.assertIn(KEY, me["activities"])
        self.assertEqual([b["activity"] for b in me["blocking"]], [KEY])
        self.assertIn("finalizeAt", me["blocking"][0])
        # The server still lets them in, so the app can fetch and submit first.
        self.assertEqual(require_activity(self.teacher.user, self.school.id, KEY), self.teacher)

    def test_the_app_confirms_and_then_the_block_takes_effect(self):
        self.block()
        response = self.acknowledge(KEY)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertNotIn(KEY, body["activities"])
        self.assertEqual(body["blocking"], [])
        with self.assertRaises(PermissionDenied):
            require_activity(self.teacher.user, self.school.id, KEY)
        entry = AccessChange.objects.get(kind="block_acknowledged")
        self.assertEqual((entry.actor, entry.target, entry.activity), (self.teacher, self.teacher, KEY))

    def test_confirming_twice_or_for_something_not_blocked_is_harmless(self):
        self.block()
        self.acknowledge(KEY)
        self.assertEqual(self.acknowledge(KEY).status_code, 200)
        self.assertEqual(self.acknowledge("teacher.timetable").status_code, 200)
        self.assertIn("teacher.timetable", self.my_activities(self.teacher))
        self.assertEqual(AccessChange.objects.filter(kind="block_acknowledged").count(), 1)

    def test_the_block_takes_effect_by_itself_if_the_app_never_reports_back(self):
        self.block()
        row = MembershipActivity.objects.get()
        self.assertIn(KEY, self.my_activities(self.teacher))
        MembershipActivity.objects.filter(pk=row.pk).update(finalize_at=timezone.now() - timedelta(seconds=1))
        me = self.me(self.teacher).json()
        self.assertNotIn(KEY, me["activities"])
        self.assertEqual(me["blocking"], [])

    @override_settings(ACCESS_BLOCK_GRACE_HOURS=2)
    def test_the_wait_is_a_setting(self):
        self.block()
        wait = MembershipActivity.objects.get().finalize_at - timezone.now()
        self.assertAlmostEqual(wait.total_seconds(), 2 * 3600, delta=30)

    def test_the_owner_can_block_immediately(self):
        self.block(mode="immediate")
        me = self.me(self.teacher).json()
        self.assertNotIn(KEY, me["activities"])
        self.assertEqual(me["blocking"], [])
        self.assertIsNone(MembershipActivity.objects.get().finalize_at)

    def test_a_grant_is_always_immediate(self):
        response = self.call("put", self.person_path(self.teacher, "finance.payroll"), self.owner, {"effect": "grant"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("finance.payroll", self.my_activities(self.teacher))
        self.assertIsNone(MembershipActivity.objects.get().finalize_at)

    def test_an_unknown_mode_is_refused(self):
        self.assertEqual(self.block(mode="tomorrow").status_code, 400)
        self.assertEqual(MembershipActivity.objects.count(), 0)

    # -- edge cases -----------------------------------------------------------------

    def test_blocking_again_after_it_took_effect_does_not_bring_it_back(self):
        self.block()
        self.acknowledge(KEY)
        self.block()  # the owner presses block a second time
        self.assertNotIn(KEY, self.my_activities(self.teacher))
        self.assertEqual(self.me(self.teacher).json()["blocking"], [])

    def test_blocking_something_they_never_had_takes_effect_at_once(self):
        # Nothing to wait for: they have no work to submit for it.
        response = self.block(key="principal.approvals")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(MembershipActivity.objects.get().finalize_at)

    def test_a_pending_block_can_be_cancelled(self):
        self.block()
        self.assertEqual(self.call("delete", self.person_path(self.teacher, KEY), self.owner).status_code, 204)
        me = self.me(self.teacher).json()
        self.assertIn(KEY, me["activities"])
        self.assertEqual(me["blocking"], [])

    def test_a_pending_block_can_be_replaced_by_a_grant(self):
        self.block()
        self.call("put", self.person_path(self.teacher, KEY), self.owner, {"effect": "grant"})
        me = self.me(self.teacher).json()
        self.assertIn(KEY, me["activities"])
        self.assertEqual(me["blocking"], [])

    def test_a_pending_block_that_has_also_expired_is_ignored(self):
        soon = timezone.now() + timedelta(days=1)
        self.block(expiresAt=soon.isoformat())
        later = soon + timedelta(seconds=1)
        self.assertEqual(services.pending_blocks(self.teacher, now=later), [])
        self.assertIn(KEY, services.effective_activities(self.teacher, now=later))

    def test_only_the_person_can_confirm_their_own_block(self):
        second = self.members["parent"]
        # A different person's confirmation cannot lift the teacher's pending state.
        self.block()
        response = self.acknowledge(KEY, who=second)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([b["activity"] for b in self.me(self.teacher).json()["blocking"]], [KEY])
        self.assertEqual(AccessChange.objects.filter(kind="block_acknowledged").count(), 0)

    def test_confirming_needs_signing_in_and_membership_and_a_sensible_body(self):
        path = f"schools/{self.school.id}/access/acknowledge/"
        self.assertEqual(self.call("post", path, None, {"activities": [KEY]}).status_code, 401)
        self.assertEqual(self.call("post", path, self.other_teacher, {"activities": [KEY]}).status_code, 403)
        for body in [{}, {"activities": []}, {"activities": "x"}]:
            self.assertEqual(self.call("post", path, self.teacher, body).status_code, 400, body)

    # -- what the owner sees and what is recorded ------------------------------------

    def test_the_owner_sees_which_blocks_are_waiting(self):
        self.block()
        self.block(key="teacher.ai", mode="immediate")
        people = {p["role"]: p for p in self.call("get", self.owner_path("people/"), self.owner).json()["people"]}
        states = {o["activity"]: o for o in people["teacher"]["overrides"]}
        self.assertEqual(states[KEY]["state"], "waiting_for_sync")
        self.assertIsNotNone(states[KEY]["takesEffectBy"])
        self.assertEqual(states["teacher.ai"]["state"], "in_force")
        self.assertIsNone(states["teacher.ai"]["takesEffectBy"])
        self.acknowledge(KEY)
        people = {p["role"]: p for p in self.call("get", self.owner_path("people/"), self.owner).json()["people"]}
        self.assertEqual({o["activity"]: o["state"] for o in people["teacher"]["overrides"]}[KEY], "in_force")

    def test_the_audit_says_how_the_block_was_made(self):
        self.block()
        self.block(key="teacher.ai", mode="immediate")
        modes = {c.activity: c.detail["mode"] for c in AccessChange.objects.filter(kind="person_block")}
        self.assertEqual(modes, {KEY: "after_sync", "teacher.ai": "immediate"})

    def test_reassigning_lets_the_new_person_start_at_once_and_the_old_one_finish_up(self):
        principal, admin = self.members["principal"], self.members["administrator"]
        response = self.call("post", self.owner_path("reassign/"), self.owner, {
            "activity": "principal.approvals", "fromMembershipId": str(principal.id), "toMembershipId": str(admin.id),
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn("principal.approvals", self.my_activities(admin))       # starts at once
        self.assertIn("principal.approvals", self.my_activities(principal))    # still finishing their work
        self.call("post", f"schools/{self.school.id}/access/acknowledge/", principal, {"activities": ["principal.approvals"]})
        self.assertNotIn("principal.approvals", self.my_activities(principal))
