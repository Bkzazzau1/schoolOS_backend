from apps.notifications.models import Notification
from apps.staff.tests.helpers import StaffTestCase
from apps.sync.models import SyncRecord

POST, COMMENT, REACTION, REPORT = "community_post", "community_comment", "community_reaction", "community_report"


def post(id="POST-1", **over):
    body = {"id": id, "title": "Sports day", "body": "Bring water.", "audience": "wholeSchool", "visibility": "schoolOnly",
            "author": "Forged Name", "role": "Forged", "comments": [{"id": "x"}], "reactions": 999}
    body.update(over)
    return body


class CommunityTestCase(StaffTestCase):
    def setUp(self):
        super().setUp()
        self.parent, self.teacher, self.principal = self.members["parent"], self.members["teacher"], self.members["principal"]

    def send(self, who, type_, id, payload=None, **kw):
        return self.push(type_, id, payload, who=who, **kw)

    def make_post(self, who=None, id="POST-1", **over):
        response = self.send(who or self.parent, POST, id, post(id, **over))
        self.ok(response)

    def stored_of(self, type_, id):
        return self.stored(type_, id).payload

    def pulled(self, who):
        self.client.force_authenticate(who.user)
        found = self.client.get("/api/v1/sync/pull/", {"school": str(self.school.id)}).json()["records"]
        return {(r["entityType"], r["entityId"]) for r in found}


class PostTests(CommunityTestCase):
    def test_parents_teachers_and_staff_can_post_and_students_cannot(self):
        for i, role in enumerate(("parent", "teacher", "staff", "accountant", "administrator", "principal", "proprietor")):
            self.make_post(self.members[role], id=f"POST-{i}")
        self.rejected(self.send(self.members["student"], POST, "POST-S", post("POST-S")), "role may not")

    def test_the_author_details_come_from_the_server_and_comments_are_dropped(self):
        self.parent.user.first_name, self.parent.user.last_name = "Aisha", "Bello"
        self.parent.user.save()
        self.make_post()
        p = self.stored_of(POST, "POST-1")
        self.assertEqual((p["author"], p["role"], p["authorMembershipId"]), ("Aisha Bello", "Parent", str(self.parent.id)))
        self.assertNotIn("comments", p)
        self.assertNotIn("reactions", p)

    def test_only_the_author_or_a_moderator_changes_or_removes_a_post(self):
        self.make_post()
        edit = lambda who, **o: self.send(who, POST, "POST-1", post(**o), operation="update")  # noqa: E731
        self.rejected(edit(self.teacher, title="Hacked"), "your own posts")
        self.ok(edit(self.parent, title="Better title"))
        self.ok(edit(self.principal, title="Moderated"))
        self.assertEqual(self.stored_of(POST, "POST-1")["title"], "Moderated")
        self.assertEqual(self.stored_of(POST, "POST-1")["authorMembershipId"], str(self.parent.id))     # still theirs
        self.rejected(self.send(self.teacher, POST, "POST-1", operation="delete"), "your own posts")
        self.ok(self.send(self.parent, POST, "POST-1", operation="delete"))
        self.assertTrue(SyncRecord.objects.get(entity_id="POST-1").deleted)

    def test_a_moderator_can_remove_any_post(self):
        self.make_post()
        self.ok(self.send(self.members["administrator"], POST, "POST-1", operation="delete"))

    def test_staff_only_posts_are_for_the_staff_side_to_make_and_read(self):
        self.rejected(self.send(self.parent, POST, "P1", post("P1", audience="staffOnly")), "Only staff")
        self.make_post(self.teacher, id="P2", audience="staffOnly")
        self.make_post(self.teacher, id="P3", audience="wholeSchool")
        self.assertEqual({i for t, i in self.pulled(self.parent) if t == POST}, {"P3"})
        self.assertEqual({i for t, i in self.pulled(self.members["staff"]) if t == POST}, {"P2", "P3"})

    def test_parents_only_posts_are_for_parents_and_moderators(self):
        self.make_post(self.principal, id="P1", audience="parentsOnly")
        seen = lambda role: {i for t, i in self.pulled(self.members[role]) if t == POST}  # noqa: E731
        self.assertEqual((seen("parent"), seen("principal")), ({"P1"}, {"P1"}))
        self.assertEqual((seen("teacher"), seen("student")), (set(), set()))

    def test_the_public_showcase_is_for_the_owner_and_principal(self):
        self.rejected(self.send(self.parent, POST, "P1", post("P1", visibility="publicShowcase")), "public showcase")
        self.make_post(self.principal, id="P2", visibility="publicShowcase")
        # An edit to an already public post does not need the leadership again.
        self.ok(self.send(self.principal, POST, "P2", post("P2", visibility="publicShowcase", title="x"), operation="update"))
        self.make_post(self.parent, id="P3")
        self.rejected(self.send(self.parent, POST, "P3", post("P3", visibility="publicShowcase"), operation="update"), "public showcase")

    def test_bad_posts_are_refused(self):
        for over in ({"title": ""}, {"body": " "}, {"audience": "everyone"}, {"visibility": "public"}, {"id": "other"}, {"title": "x" * 201}):
            self.rejected(self.send(self.parent, POST, "P1", {**post("P1"), **over}))


class CommentTests(CommunityTestCase):
    def setUp(self):
        super().setUp()
        self.make_post()

    def comment(self, who, id="C-1", post_id="POST-1", text="Nice!", **kw):
        return self.send(who, COMMENT, id, {"id": id, "postId": post_id, "text": text, "author": "Forged"}, **kw)

    def test_anyone_who_can_see_a_post_can_comment_and_the_name_is_the_servers(self):
        self.ok(self.comment(self.teacher))
        c = self.stored_of(COMMENT, "C-1")
        self.assertEqual((c["postId"], c["text"], c["authorMembershipId"]), ("POST-1", "Nice!", str(self.teacher.id)))
        self.assertNotEqual(c["author"], "Forged")
        self.rejected(self.comment(self.members["student"], id="C-2"), "role may not")

    def test_two_people_commenting_never_touch_each_others_records(self):
        self.ok(self.comment(self.teacher, id="C-1"))
        self.ok(self.comment(self.principal, id="C-2", text="Agreed"))
        self.assertEqual(SyncRecord.objects.filter(entity_type=COMMENT).count(), 2)

    def test_a_comment_needs_a_visible_post(self):
        self.rejected(self.comment(self.teacher, post_id="GHOST"), "does not exist")
        self.make_post(self.teacher, id="STAFF", audience="staffOnly")
        self.rejected(self.comment(self.parent, id="C-9", post_id="STAFF"), "does not exist")
        self.ok(self.comment(self.members["staff"], id="C-9", post_id="STAFF"))
        self.rejected(self.comment(self.teacher, id="C-8", text=""), "text")
        self.rejected(self.comment(self.teacher, id="C-7", text="x" * 2001), "too long")

    def test_only_the_author_edits_and_the_author_or_a_moderator_removes(self):
        self.ok(self.comment(self.teacher))
        self.rejected(self.comment(self.principal, text="Edited", operation="update"), "your own comments")
        self.ok(self.comment(self.teacher, text="Edited", operation="update"))
        self.make_post(self.principal, id="POST-2")
        self.rejected(self.comment(self.teacher, post_id="POST-2", operation="update"), "another post")
        self.rejected(self.send(self.parent, COMMENT, "C-1", operation="delete"), "your own comments")
        self.ok(self.send(self.principal, COMMENT, "C-1", operation="delete"))

    def test_comments_follow_their_posts_audience_when_pulled(self):
        self.make_post(self.teacher, id="STAFF", audience="staffOnly")
        self.ok(self.comment(self.teacher, id="C-S", post_id="STAFF"))
        self.ok(self.comment(self.teacher, id="C-P", post_id="POST-1"))
        self.assertEqual({i for t, i in self.pulled(self.parent) if t == COMMENT}, {"C-P"})
        self.assertEqual({i for t, i in self.pulled(self.members["staff"]) if t == COMMENT}, {"C-S", "C-P"})

    def test_when_a_post_is_removed_its_comments_are_no_longer_sent_to_ordinary_members(self):
        self.ok(self.comment(self.teacher))
        self.ok(self.send(self.parent, POST, "POST-1", operation="delete"))
        self.assertNotIn((COMMENT, "C-1"), self.pulled(self.teacher))
        self.assertIn((COMMENT, "C-1"), self.pulled(self.principal))


class ReactionTests(CommunityTestCase):
    def setUp(self):
        super().setUp()
        self.make_post()

    def react(self, who, post_id="POST-1", id=None, **kw):
        return self.send(who, REACTION, id or f"{post_id}:{who.id}", {"postId": post_id, "kind": "like"}, **kw)

    def test_one_reaction_per_person_per_post_and_only_as_yourself(self):
        self.ok(self.react(self.teacher))
        self.assertEqual(self.stored_of(REACTION, f"POST-1:{self.teacher.id}")["authorMembershipId"], str(self.teacher.id))
        self.assertEqual(self.react(self.teacher).status_code, 409)                         # already reacted
        self.rejected(self.react(self.teacher, id=f"POST-1:{self.principal.id}"), "your membership id")
        self.rejected(self.react(self.teacher, id="whatever"), "your membership id")
        self.rejected(self.react(self.teacher, post_id="OTHER", id=f"ELSE:{self.teacher.id}"), "must be the post id")

    def test_taking_it_back_deletes_it_and_a_reaction_needs_a_visible_post(self):
        self.ok(self.react(self.teacher))
        self.ok(self.send(self.teacher, REACTION, f"POST-1:{self.teacher.id}", operation="delete"))
        self.rejected(self.react(self.teacher, post_id="GHOST"), "does not exist")
        self.rejected(self.send(self.members["student"], REACTION, f"POST-1:{self.members['student'].id}", {"postId": "POST-1", "kind": "like"}), "role may not")

    def test_only_known_kinds_and_only_posts_you_can_see(self):
        self.rejected(self.send(self.teacher, REACTION, f"POST-1:{self.teacher.id}", {"postId": "POST-1", "kind": "boo"}), "kind")
        self.make_post(self.teacher, id="STAFF", audience="staffOnly")
        self.rejected(self.react(self.parent, post_id="STAFF"), "does not exist")


class ReportTests(CommunityTestCase):
    def setUp(self):
        super().setUp()
        self.make_post()

    def report(self, who, id="REP-1", post_id="POST-1", **kw):
        return self.send(who, REPORT, id, {"id": id, "postId": post_id, "status": "actioned", "reason": "Rude"}, **kw)

    def test_anyone_who_sees_a_post_can_report_it_and_it_starts_awaiting_review(self):
        self.ok(self.report(self.teacher))
        r = self.stored_of(REPORT, "REP-1")
        self.assertEqual((r["status"], r["reportedByMembershipId"]), ("awaitingReview", str(self.teacher.id)))
        self.rejected(self.report(self.teacher, id="REP-2", post_id="GHOST"), "does not exist")

    def test_moderators_are_told(self):
        self.ok(self.report(self.teacher))
        told = set(Notification.objects.filter(kind="community_report").values_list("recipient_id", flat=True))
        self.assertEqual(told, {self.owner.id, self.principal.id, self.members["administrator"].id})

    def test_only_a_moderator_settles_it(self):
        self.ok(self.report(self.teacher))
        payload = {"id": "REP-1", "status": "actioned"}
        self.rejected(self.send(self.teacher, REPORT, "REP-1", payload, operation="update"), "moderator")
        self.rejected(self.send(self.principal, REPORT, "REP-1", {"id": "REP-1", "status": "paid"}, operation="update"), "status")
        self.ok(self.send(self.principal, REPORT, "REP-1", payload, operation="update"))
        r = self.stored_of(REPORT, "REP-1")
        self.assertEqual((r["status"], r["settledByMembershipId"], r["postId"]), ("actioned", str(self.principal.id), "POST-1"))

    def test_only_moderators_and_the_reporter_see_a_report(self):
        self.ok(self.report(self.teacher))
        for role, sees in (("teacher", True), ("principal", True), ("proprietor", True), ("parent", False), ("staff", False)):
            self.assertEqual((REPORT, "REP-1") in self.pulled(self.members[role]), sees, role)
