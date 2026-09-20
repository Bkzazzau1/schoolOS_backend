from datetime import timedelta

from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

from apps.access import catalog
from apps.access.models import AccessChange, MembershipActivity
from apps.access.permissions import require_activity

from .helpers import AccessTestCase


class MyAccessTests(AccessTestCase):
    def test_each_role_sees_its_own_defaults(self):
        for role, member in self.members.items():
            response = self.me(member)
            self.assertEqual(response.status_code, 200, role)
            body = response.json()
            self.assertEqual((body["membershipId"], body["role"]), (str(member.id), role))
            self.assertEqual(set(body["activities"]), catalog.default_keys(role), role)
            self.assertEqual(body["activities"], sorted(body["activities"]))

    def test_a_change_by_the_owner_shows_up_on_their_next_read(self):
        teacher = self.members["teacher"]
        self.assertIn("teacher.cbt", self.my_activities(teacher))
        self.call("put", self.person_path(teacher, "teacher.cbt"), self.owner, {"effect": "block", "mode": "immediate"})
        self.assertNotIn("teacher.cbt", self.my_activities(teacher))
        self.call("delete", self.person_path(teacher, "teacher.cbt"), self.owner)
        self.assertIn("teacher.cbt", self.my_activities(teacher))

    def test_signing_in_and_membership_are_required(self):
        path = f"schools/{self.school.id}/access/me/"
        self.assertEqual(self.call("get", path).status_code, 401)
        self.assertEqual(self.call("get", path, self.other_teacher).status_code, 403)  # another school's person
        teacher = self.members["teacher"]
        teacher.is_active = False
        teacher.save()
        self.assertEqual(self.call("get", path, teacher).status_code, 403)

    def test_someone_cannot_read_another_persons_access(self):
        # The endpoint only ever returns the caller's own membership.
        self.assertEqual(self.me(self.members["teacher"]).json()["membershipId"], str(self.members["teacher"].id))


class OwnerOnlyTests(AccessTestCase):
    def endpoints(self):
        teacher = self.members["teacher"]
        yield "get", self.owner_path("catalog/"), None
        yield "get", self.owner_path("roles/"), None
        yield "put", self.owner_path("roles/teacher/"), {"activities": sorted(catalog.default_keys("teacher"))}
        yield "delete", self.owner_path("roles/teacher/"), None
        yield "get", self.owner_path("people/"), None
        yield "put", self.person_path(teacher, "teacher.cbt"), {"effect": "block", "mode": "immediate"}
        yield "delete", self.person_path(teacher, "teacher.cbt"), None
        yield "post", self.owner_path("reassign/"), {
            "activity": "principal.ai", "fromMembershipId": str(self.members["principal"].id),
            "toMembershipId": str(self.members["administrator"].id),
        }
        yield "get", self.owner_path("audit/"), None

    def test_every_management_endpoint_refuses_everyone_but_the_owner(self):
        for role, member in self.members.items():
            if role == "proprietor":
                continue
            for method, path, data in self.endpoints():
                self.assertEqual(self.call(method, path, member, data).status_code, 403, f"{role} {method} {path}")
        for method, path, data in self.endpoints():
            self.assertEqual(self.call(method, path, None, data).status_code, 401, f"anonymous {method} {path}")
            # An owner of a different school has no power here either.
            self.assertEqual(self.call(method, path, self.other_owner, data).status_code, 403, f"other owner {method} {path}")
        self.assertEqual(MembershipActivity.objects.count(), 0)
        self.assertEqual(AccessChange.objects.count(), 0)


class CatalogAndRolesApiTests(AccessTestCase):
    def test_the_catalog_lists_every_activity_grouped(self):
        body = self.call("get", self.owner_path("catalog/"), self.owner).json()
        self.assertEqual(
            [g["area"] for g in body["groups"]],
            ["Owner", "Principal", "Administrator", "Finance office", "Teacher", "Parent", "School life", "General"],
        )
        every = [a for g in body["groups"] for a in g["activities"]]
        self.assertEqual(len(every), 106)
        payroll = next(a for a in every if a["key"] == "finance.payroll")
        self.assertEqual(
            payroll,
            {"key": "finance.payroll", "label": "Payroll Handoff", "essential": False, "grantable": True,
             "sensitive": True, "defaultRoles": ["accountant"], "rolesInThisSchool": ["accountant"]},
        )
        self.assertFalse(next(a for a in every if a["key"] == "owner.access")["grantable"])

    def test_the_catalog_reflects_this_schools_role_changes(self):
        keys = sorted(catalog.default_keys("teacher") | {"finance.receipts"})
        self.call("put", self.owner_path("roles/teacher/"), self.owner, {"activities": keys})
        body = self.call("get", self.owner_path("catalog/"), self.owner).json()
        receipts = next(a for g in body["groups"] for a in g["activities"] if a["key"] == "finance.receipts")
        self.assertEqual(receipts["defaultRoles"], ["accountant"])
        self.assertEqual(receipts["rolesInThisSchool"], ["accountant", "teacher"])

    def test_roles_show_what_each_gets_and_whether_it_was_changed(self):
        roles = {r["role"]: r for r in self.call("get", self.owner_path("roles/"), self.owner).json()["roles"]}
        self.assertNotIn("proprietor", roles)
        self.assertEqual(set(roles), {"administrator", "principal", "teacher", "accountant", "parent", "student", "staff"})
        self.assertFalse(any(r["customized"] for r in roles.values()))
        self.call("put", self.owner_path("roles/teacher/"), self.owner,
                  {"activities": sorted(catalog.default_keys("teacher") - {"teacher.cbt"})})
        roles = {r["role"]: r for r in self.call("get", self.owner_path("roles/"), self.owner).json()["roles"]}
        self.assertTrue(roles["teacher"]["customized"])
        self.assertNotIn("teacher.cbt", roles["teacher"]["activities"])
        self.assertFalse(roles["principal"]["customized"])

    def test_setting_and_resetting_a_role(self):
        keys = sorted(catalog.default_keys("teacher") - {"teacher.cbt"})
        response = self.call("put", self.owner_path("roles/teacher/"), self.owner, {"activities": keys})
        self.assertEqual((response.status_code, response.json()["activities"]), (200, keys))
        self.assertNotIn("teacher.cbt", self.my_activities(self.members["teacher"]))
        response = self.call("delete", self.owner_path("roles/teacher/"), self.owner)
        self.assertEqual(set(response.json()["activities"]), catalog.default_keys("teacher"))
        self.assertIn("teacher.cbt", self.my_activities(self.members["teacher"]))

    def test_bad_role_changes_are_400_with_a_message_and_change_nothing(self):
        teacher = sorted(catalog.default_keys("teacher"))
        for role, keys, fragment in [
            ("teacher", [k for k in teacher if k != "teacher.dashboard"], "landing screen"),
            ("teacher", teacher + ["owner.access"], "owner"),
            ("teacher", teacher + ["bogus.key"], "not an activity"),
            ("proprietor", ["owner.overview"], "owner"),
            ("emperor", [], "not a role"),
        ]:
            response = self.call("put", self.owner_path(f"roles/{role}/"), self.owner, {"activities": keys})
            self.assertEqual(response.status_code, 400, (role, fragment))
            self.assertEqual(response.json()["code"], "access_error")
            self.assertIn(fragment, response.json()["message"])
        self.assertEqual(self.school.role_activities.count(), 0)
        self.assertEqual(self.call("put", self.owner_path("roles/teacher/"), self.owner, {"activities": "x"}).status_code, 400)


class PeopleApiTests(AccessTestCase):
    def test_the_owner_sees_everyone_with_what_they_can_see_and_what_was_changed(self):
        teacher = self.members["teacher"]
        self.call("put", self.person_path(teacher, "teacher.cbt"), self.owner, {"effect": "block", "mode": "immediate", "note": "Exam week"})
        self.call("put", self.person_path(teacher, "finance.payroll"), self.owner, {"effect": "grant"})
        people = {p["role"]: p for p in self.call("get", self.owner_path("people/"), self.owner).json()["people"]}
        self.assertEqual(set(people), {r for r in self.members})  # only this school
        one = people["teacher"]
        self.assertEqual((one["membershipId"], one["email"]), (str(teacher.id), "teacher@school.ng"))
        self.assertNotIn("teacher.cbt", one["activities"])
        self.assertIn("finance.payroll", one["activities"])
        self.assertEqual(
            sorted((o["activity"], o["effect"], o["note"]) for o in one["overrides"]),
            [("finance.payroll", "grant", ""), ("teacher.cbt", "block", "Exam week")],
        )
        self.assertEqual(people["principal"]["overrides"], [])

    def test_inactive_members_are_left_out(self):
        teacher = self.members["teacher"]
        teacher.is_active = False
        teacher.save()
        roles = {p["role"] for p in self.call("get", self.owner_path("people/"), self.owner).json()["people"]}
        self.assertNotIn("teacher", roles)


class PersonActivityApiTests(AccessTestCase):
    def test_block_grant_and_clear(self):
        principal = self.members["principal"]
        response = self.call("put", self.person_path(principal, "principal.ai"), self.owner, {"effect": "block", "mode": "immediate"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual((response.json()["activity"], response.json()["effect"]), ("principal.ai", "block"))
        self.assertEqual(self.call("put", self.person_path(principal, "owner.payroll"), self.owner, {"effect": "grant"}).status_code, 200)
        self.assertEqual(self.call("delete", self.person_path(principal, "principal.ai"), self.owner).status_code, 204)
        self.assertEqual(self.call("delete", self.person_path(principal, "principal.ai"), self.owner).status_code, 404)

    def test_an_end_date_is_kept_and_a_past_one_is_refused(self):
        teacher = self.members["teacher"]
        soon = (timezone.now() + timedelta(days=3)).isoformat()
        ok = self.call("put", self.person_path(teacher, "finance.payroll"), self.owner, {"effect": "grant", "expiresAt": soon})
        self.assertEqual(ok.status_code, 200)
        self.assertIsNotNone(ok.json()["expiresAt"])
        past = (timezone.now() - timedelta(days=1)).isoformat()
        bad = self.call("put", self.person_path(teacher, "finance.reports"), self.owner, {"effect": "grant", "expiresAt": past})
        self.assertEqual(bad.status_code, 400)
        self.assertNotIn("finance.reports", self.my_activities(teacher))

    def test_refused_changes_are_400_with_a_message(self):
        teacher, principal = self.members["teacher"], self.members["principal"]
        for member, activity, body, fragment in [
            (teacher, "teacher.dashboard", {"effect": "block", "mode": "immediate"}, "landing screen"),
            (principal, "owner.access", {"effect": "grant"}, "owner"),
            (self.owner, "owner.payroll", {"effect": "block", "mode": "immediate"}, "owner"),
            (teacher, "nonsense.thing", {"effect": "block", "mode": "immediate"}, "not an activity"),
            (self.other_teacher, "teacher.cbt", {"effect": "block", "mode": "immediate"}, "not a member"),
        ]:
            response = self.call("put", self.person_path(member, activity), self.owner, body)
            self.assertEqual(response.status_code, 400, (activity, fragment))
            self.assertIn(fragment, response.json()["message"])
        self.assertEqual(MembershipActivity.objects.count(), 0)

    def test_malformed_bodies_are_400(self):
        path = self.person_path(self.members["teacher"], "teacher.cbt")
        for body in [{}, {"effect": "maybe"}, {"effect": "block", "mode": "immediate", "note": "x" * 201}, {"effect": "block", "mode": "immediate", "expiresAt": "soon"}]:
            self.assertEqual(self.call("put", path, self.owner, body).status_code, 400, body)

    def test_the_owner_cannot_reach_into_another_school_by_id(self):
        # A membership id from another school is simply "not a member of this school".
        response = self.call("put", self.person_path(self.other_teacher, "teacher.cbt"), self.owner, {"effect": "block", "mode": "immediate"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(MembershipActivity.objects.count(), 0)


class ReassignAndAuditApiTests(AccessTestCase):
    def reassign(self, activity, giver, receiver, **extra):
        return self.call("post", self.owner_path("reassign/"), self.owner, {
            "activity": activity, "fromMembershipId": str(giver.id), "toMembershipId": str(receiver.id), "mode": "immediate", **extra,
        })

    def test_reassigning_moves_an_activity_and_is_visible_to_both_people(self):
        principal, admin = self.members["principal"], self.members["administrator"]
        response = self.reassign("principal.approvals", principal, admin, note="On leave")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("principal.approvals", self.my_activities(principal))
        self.assertIn("principal.approvals", self.my_activities(admin))

    def test_a_refused_reassign_changes_nothing(self):
        principal, teacher = self.members["principal"], self.members["teacher"]
        response = self.reassign("principal.approvals", teacher, principal)  # the teacher never had it
        self.assertEqual(response.status_code, 400)
        response = self.reassign("principal.dashboard", principal, teacher)  # a landing screen
        self.assertEqual(response.status_code, 400)
        self.assertEqual(MembershipActivity.objects.count(), 0)
        self.assertEqual(self.call("post", self.owner_path("reassign/"), self.owner, {"activity": "x"}).status_code, 400)

    def test_the_audit_lists_changes_newest_first_with_names(self):
        teacher = self.members["teacher"]
        self.call("put", self.person_path(teacher, "teacher.cbt"), self.owner, {"effect": "block", "mode": "immediate"})
        self.call("put", self.person_path(teacher, "finance.payroll"), self.owner, {"effect": "grant"})
        changes = self.call("get", self.owner_path("audit/"), self.owner).json()["changes"]
        self.assertEqual([c["kind"] for c in changes], ["person_grant", "person_block"])
        self.assertEqual((changes[0]["by"], changes[0]["for"], changes[0]["activity"]),
                         ("proprietor@school.ng", "teacher@school.ng", "finance.payroll"))

    def test_the_audit_is_limited_and_only_shows_this_school(self):
        for _ in range(3):
            self.call("put", self.person_path(self.members["teacher"], "teacher.cbt"), self.owner, {"effect": "block", "mode": "immediate"})
        self.call("put", f"owner/schools/{self.other_school.id}/access/people/{self.other_teacher.id}/activities/teacher.cbt/",
                  self.other_owner, {"effect": "block", "mode": "immediate"})
        self.assertEqual(len(self.call("get", self.owner_path("audit/") + "?limit=2", self.owner).json()["changes"]), 2)
        self.assertEqual(len(self.call("get", self.owner_path("audit/") + "?limit=abc", self.owner).json()["changes"]), 3)
        self.assertEqual(len(self.call("get", self.owner_path("audit/", self.other_school), self.other_owner).json()["changes"]), 1)


class EnforcementHelperTests(AccessTestCase):
    """What other features call so a blocked person cannot reach data by API."""

    def test_require_activity_lets_through_people_who_have_it_and_refuses_the_rest(self):
        teacher, accountant = self.members["teacher"], self.members["accountant"]
        self.assertEqual(require_activity(accountant.user, self.school.id, "finance.payroll"), accountant)
        with self.assertRaises(PermissionDenied):
            require_activity(teacher.user, self.school.id, "finance.payroll")

    def test_blocking_takes_effect_on_the_server_not_just_in_the_menu(self):
        accountant = self.members["accountant"]
        self.call("put", self.person_path(accountant, "finance.payroll"), self.owner, {"effect": "block", "mode": "immediate"})
        with self.assertRaises(PermissionDenied):
            require_activity(accountant.user, self.school.id, "finance.payroll")
        self.call("delete", self.person_path(accountant, "finance.payroll"), self.owner)
        self.assertEqual(require_activity(accountant.user, self.school.id, "finance.payroll"), accountant)

    def test_someone_from_another_school_never_passes(self):
        with self.assertRaises(PermissionDenied):
            require_activity(self.other_teacher.user, self.school.id, "teacher.cbt")


class TwoRolesTests(AccessTestCase):
    """A person can be a teacher and a parent at the same school."""

    def setUp(self):
        super().setUp()
        self.teacher = self.members["teacher"]
        self.also_parent = self.teacher.__class__.objects.create(
            user=self.teacher.user, school=self.school, role="parent"
        )

    def me_as(self, membership=None):
        suffix = f"?membership={membership.id}" if membership else ""
        return self.call("get", f"schools/{self.school.id}/access/me/{suffix}", self.teacher)

    def test_the_server_asks_which_role_instead_of_guessing(self):
        response = self.me_as()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "membership_required")
        self.assertEqual({m["role"] for m in response.json()["memberships"]}, {"teacher", "parent"})

    def test_each_role_has_its_own_access(self):
        as_teacher = self.me_as(self.teacher).json()
        as_parent = self.me_as(self.also_parent).json()
        self.assertEqual((as_teacher["role"], as_parent["role"]), ("teacher", "parent"))
        self.assertIn("teacher.cbt", as_teacher["activities"])
        self.assertNotIn("teacher.cbt", as_parent["activities"])
        self.assertIn("parent.finance", as_parent["activities"])
        self.assertNotIn("parent.finance", as_teacher["activities"])

    def test_blocking_one_role_leaves_the_other_untouched(self):
        self.call("put", self.person_path(self.also_parent, "parent.finance"), self.owner,
                  {"effect": "block", "mode": "immediate"})
        self.assertNotIn("parent.finance", self.me_as(self.also_parent).json()["activities"])
        self.assertIn("teacher.cbt", self.me_as(self.teacher).json()["activities"])

    def test_a_person_can_only_name_their_own_memberships(self):
        for value in [str(self.members["principal"].id), str(self.other_teacher.id), "nonsense"]:
            response = self.call("get", f"schools/{self.school.id}/access/me/?membership={value}", self.teacher)
            self.assertEqual(response.status_code, 403, value)

    def test_the_enforcement_helper_needs_to_know_which_role_too(self):
        from rest_framework.exceptions import APIException

        with self.assertRaises(APIException) as caught:
            require_activity(self.teacher.user, self.school.id, "teacher.cbt")
        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(require_activity(self.teacher.user, self.school.id, "teacher.cbt", self.teacher.id), self.teacher)
        with self.assertRaises(PermissionDenied):
            require_activity(self.teacher.user, self.school.id, "teacher.cbt", self.also_parent.id)

    def test_the_owner_who_is_also_a_parent_is_still_recognised_as_the_owner(self):
        self.owner.__class__.objects.create(user=self.owner.user, school=self.school, role="parent")
        self.assertEqual(self.call("get", self.owner_path("catalog/"), self.owner).status_code, 200)
