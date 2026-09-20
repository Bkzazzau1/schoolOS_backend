from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from apps.notifications.models import Notification
from apps.notifications.services import notify, notify_many
from apps.schools.models import Membership, Role, School

User = get_user_model()


class InboxTests(APITestCase):
    def setUp(self):
        self.school = School.objects.create(name="A", slug="a")
        self.other_school = School.objects.create(name="B", slug="b")

        def member(email, role, school=None):
            user = User.objects.create_user(email, "a-long-test-password-1")
            return Membership.objects.create(user=user, school=school or self.school, role=role)

        self.ada = member("ada@a.ng", Role.TEACHER)
        self.bola = member("bola@a.ng", Role.PARENT)
        self.outsider = member("out@b.ng", Role.TEACHER, self.other_school)

    def call(self, method, path, who=None, data=None):
        self.client.force_authenticate(who.user if who else None)
        return getattr(self.client, method)(f"/api/v1/schools/{self.school.id}/notifications/{path}", data, format="json")

    def test_a_person_sees_only_their_own_messages_newest_first(self):
        notify(self.ada, "x", "First", "one")
        notify(self.bola, "x", "Bola's", "private")
        notify(self.ada, "access_changed", "Second", "two", {"activities": ["teacher.cbt"]})
        body = self.call("get", "", self.ada).json()
        self.assertEqual(body["unread"], 2)
        self.assertEqual([n["title"] for n in body["notifications"]], ["Second", "First"])
        first = body["notifications"][0]
        self.assertEqual((first["kind"], first["message"], first["read"]), ("access_changed", "two", False))
        self.assertEqual(first["data"], {"activities": ["teacher.cbt"]})
        self.assertEqual([n["title"] for n in self.call("get", "", self.bola).json()["notifications"]], ["Bola's"])

    def test_a_persons_messages_are_never_visible_to_someone_else(self):
        notify(self.bola, "x", "Private", "for bola only")
        self.assertNotIn("Private", str(self.call("get", "", self.ada).json()))
        self.assertEqual(self.call("get", "", self.outsider).status_code, 403)   # another school's person
        self.assertEqual(self.call("get", "", None).status_code, 401)

    def test_unread_filter_and_limit(self):
        for i in range(5):
            notify(self.ada, "x", f"n{i}", "m")
        self.call("post", f"{Notification.objects.filter(recipient=self.ada).first().id}/read/", self.ada)
        self.assertEqual(len(self.call("get", "?unread=1", self.ada).json()["notifications"]), 4)
        self.assertEqual(len(self.call("get", "?limit=2", self.ada).json()["notifications"]), 2)
        self.assertEqual(len(self.call("get", "?limit=abc", self.ada).json()["notifications"]), 5)
        self.assertEqual(len(self.call("get", "?limit=9999", self.ada).json()["notifications"]), 5)

    def test_marking_read_lowers_the_unread_count_and_is_repeatable(self):
        notify(self.ada, "x", "a", "m")
        notify(self.ada, "x", "b", "m")
        one = Notification.objects.filter(recipient=self.ada).first()
        self.assertEqual(self.call("post", f"{one.id}/read/", self.ada).status_code, 200)
        self.assertEqual(self.call("post", f"{one.id}/read/", self.ada).status_code, 200)
        body = self.call("get", "", self.ada).json()
        self.assertEqual(body["unread"], 1)
        self.assertEqual(sum(n["read"] for n in body["notifications"]), 1)

    def test_nobody_can_mark_someone_elses_message_read(self):
        notify(self.bola, "x", "Private", "m")
        theirs = Notification.objects.get()
        self.assertEqual(self.call("post", f"{theirs.id}/read/", self.ada).status_code, 404)
        self.assertEqual(self.call("post", "999999/read/", self.ada).status_code, 404)
        theirs.refresh_from_db()
        self.assertIsNone(theirs.read_at)

    def test_read_all_only_touches_the_callers_messages(self):
        notify(self.ada, "x", "a", "m")
        notify(self.ada, "x", "b", "m")
        notify(self.bola, "x", "c", "m")
        self.assertEqual(self.call("post", "read-all/", self.ada).json(), {"marked": 2})
        self.assertEqual(self.call("get", "", self.ada).json()["unread"], 0)
        self.assertEqual(self.call("get", "", self.bola).json()["unread"], 1)

    def test_someone_with_two_roles_must_say_which_and_has_a_separate_inbox_for_each(self):
        as_parent = Membership.objects.create(user=self.ada.user, school=self.school, role=Role.PARENT)
        notify(as_parent, "x", "For the parent role", "m")
        notify(self.ada, "x", "For the teacher role", "m")
        # Not naming one is refused, never guessed, and the answer lists the choices.
        response = self.call("get", "", self.ada)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "membership_required")
        self.assertEqual({m["role"] for m in response.json()["memberships"]}, {"teacher", "parent"})
        teacher_box = self.call("get", f"?membership={self.ada.id}", self.ada).json()["notifications"]
        parent_box = self.call("get", f"?membership={as_parent.id}", self.ada).json()["notifications"]
        self.assertEqual([n["title"] for n in teacher_box], ["For the teacher role"])
        self.assertEqual([n["title"] for n in parent_box], ["For the parent role"])
        # Marking read is per role too.
        self.assertEqual(self.call("post", f"read-all/?membership={as_parent.id}", self.ada).json(), {"marked": 1})
        self.assertEqual(self.call("get", f"?membership={self.ada.id}", self.ada).json()["unread"], 1)

    def test_nobody_can_ask_for_someone_elses_membership(self):
        notify(self.bola, "x", "Private", "m")
        for value in [str(self.bola.id), str(self.outsider.id), "not-a-uuid", "11111111-1111-1111-1111-111111111111"]:
            self.assertEqual(self.call("get", f"?membership={value}", self.ada).status_code, 403, value)

    def test_notifying_many_and_long_titles(self):
        self.assertEqual(notify_many([self.ada, self.bola], "x", "T" * 300, "m"), 2)
        self.assertEqual(Notification.objects.count(), 2)
        self.assertTrue(all(len(n.title) == 120 for n in Notification.objects.all()))
        self.assertEqual(notify_many([], "x", "t", "m"), 0)
