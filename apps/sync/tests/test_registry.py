from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, override_settings
from rest_framework.test import APITestCase

from apps.core.errors import Rejected
from apps.schools.models import Membership, Role, School
from apps.sync import registry
from apps.sync.models import MutationLog, SyncRecord

User = get_user_model()


class NoteHandler(registry.EntityHandler):
    """A stand-in feature: teachers may write notes, and the server stamps them."""

    entity_type = "test_note"
    roles = frozenset({Role.TEACHER})
    allow_delete = False

    def clean(self, ctx):
        title = str(ctx.payload.get("title", "")).strip()
        if not title:
            raise Rejected("A note needs a title.")
        return {
            "title": title.upper(),
            "writtenBy": str(ctx.membership.id),  # server-owned, never from the app
            "wasUpdate": ctx.existing is not None,
            "at": ctx.now,
        }


class RegistryRulesTests(SimpleTestCase):
    def test_a_type_can_only_have_one_handler_and_needs_a_name(self):
        registry.register(NoteHandler())
        self.addCleanup(registry._handlers.pop, "test_note")
        with self.assertRaises(ValueError):
            registry.register(NoteHandler())

        class Nameless(registry.EntityHandler):
            pass

        with self.assertRaises(ValueError):
            registry.register(Nameless())


class HandlerFlowTests(APITestCase):
    def setUp(self):
        self.handler = NoteHandler()
        registry.register(self.handler)
        self.addCleanup(registry._handlers.pop, "test_note")
        self.school = School.objects.create(name="A", slug="a")
        self.teacher_user = User.objects.create_user("t@a.ng", "a-long-test-password-1")
        self.teacher = Membership.objects.create(user=self.teacher_user, school=self.school, role=Role.TEACHER)
        self.owner_user = User.objects.create_user("o@a.ng", "a-long-test-password-1")
        self.owner = Membership.objects.create(user=self.owner_user, school=self.school, role=Role.PROPRIETOR)
        self.n = 0

    def push(self, who, operation="create", entity_id="n1", payload=None):
        self.n += 1
        self.client.force_authenticate(who.user)
        body = {
            "id": f"m{self.n}", "tenantId": str(self.school.id), "membershipId": str(who.id),
            "entityType": "test_note", "entityId": entity_id, "operation": operation,
        }
        if operation != "delete":
            body["payload"] = payload if payload is not None else {"title": "hello"}
        return self.client.post("/api/v1/sync/push/", body, format="json")

    def test_the_handler_decides_who_may_write_and_a_refusal_is_recorded(self):
        response = self.push(self.owner)
        self.assertEqual(response.status_code, 422)
        self.assertIn("role", response.json()["message"])
        self.assertEqual(SyncRecord.objects.count(), 0)
        self.assertEqual(MutationLog.objects.get().disposition, "rejected")

    def test_only_what_the_handler_returns_is_stored(self):
        response = self.push(self.teacher, payload={"title": "hello", "writtenBy": "someone-else", "extra": 1})
        self.assertEqual(response.status_code, 200)
        stored = SyncRecord.objects.get().payload
        self.assertEqual(stored["title"], "HELLO")
        self.assertEqual(stored["writtenBy"], str(self.teacher.id))
        self.assertNotIn("extra", stored)
        self.assertFalse(stored["wasUpdate"])

    def test_the_handler_sees_the_stored_record_on_update(self):
        self.push(self.teacher)
        self.push(self.teacher, operation="update", payload={"title": "again"})
        stored = SyncRecord.objects.get()
        self.assertEqual(stored.payload["title"], "AGAIN")
        self.assertTrue(stored.payload["wasUpdate"])
        self.assertEqual(stored.version, 2)

    def test_a_rejection_from_clean_saves_nothing_and_says_why(self):
        response = self.push(self.teacher, payload={"title": "  "})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["message"], "A note needs a title.")
        self.assertEqual(SyncRecord.objects.count(), 0)

    def test_a_rejected_update_leaves_the_record_as_it_was(self):
        self.push(self.teacher)
        response = self.push(self.teacher, operation="update", payload={"title": ""})
        self.assertEqual(response.status_code, 422)
        stored = SyncRecord.objects.get()
        self.assertEqual((stored.payload["title"], stored.version), ("HELLO", 1))

    def test_delete_is_refused_unless_the_handler_allows_it(self):
        self.push(self.teacher)
        self.assertEqual(self.push(self.teacher, operation="delete").status_code, 422)
        self.assertFalse(SyncRecord.objects.get().deleted)
        self.handler.allow_delete = True
        self.assertEqual(self.push(self.teacher, operation="delete").status_code, 200)
        self.assertTrue(SyncRecord.objects.get().deleted)

    @override_settings(SYNC_ALLOW_UNLISTED_ENTITY_TYPES=True)
    def test_allowing_unlisted_types_never_bypasses_a_types_handler(self):
        self.assertEqual(self.push(self.owner).status_code, 422)
        self.assertEqual(SyncRecord.objects.count(), 0)

    def test_authorization_is_checked_before_conflicts_are_revealed(self):
        self.push(self.teacher)
        # The owner is refused outright, and learns nothing about the record.
        response = self.push(self.owner)
        self.assertEqual(response.status_code, 422)
        self.assertIsNone(response.json()["serverVersion"])
