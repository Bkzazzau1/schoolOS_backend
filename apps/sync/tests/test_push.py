from django.contrib.auth import get_user_model
from django.test import override_settings
from rest_framework.test import APITestCase

from apps.schools.models import Membership, Role, School

from apps.sync.models import MutationLog, SyncRecord

User = get_user_model()
PUSH = "/api/v1/sync/push/"


class PushTests(APITestCase):
    def setUp(self):
        self.school = School.objects.create(name="A", slug="a")
        self.other_school = School.objects.create(name="B", slug="b")
        self.owner_user = User.objects.create_user("owner@a.ng", "a-long-test-password-1")
        self.owner = Membership.objects.create(user=self.owner_user, school=self.school, role=Role.PROPRIETOR)
        self.teacher_user = User.objects.create_user("teacher@a.ng", "a-long-test-password-1")
        self.teacher = Membership.objects.create(user=self.teacher_user, school=self.school, role=Role.TEACHER)
        self.client.force_authenticate(self.owner_user)
        self._n = 0

    def mutation(self, *, membership=None, school=None, operation="create",
                 entity_type="school_event", entity_id="e1", payload=None,
                 base_version=None, mutation_id=None):
        self._n += 1
        body = {
            "id": mutation_id or f"m{self._n}",
            "tenantId": str((school or self.school).id),
            "membershipId": str((membership or self.owner).id),
            "entityType": entity_type,
            "entityId": entity_id,
            "operation": operation,
        }
        if payload is not None or operation != "delete":
            body["payload"] = payload if payload is not None else {"title": "Assembly"}
        if base_version is not None:
            body["baseVersion"] = base_version
        return body

    def push(self, **kwargs):
        return self.client.post(PUSH, self.mutation(**kwargs), format="json")

    # -- create / update / delete ------------------------------------------

    def test_create_then_update_then_delete_bump_the_version(self):
        created = self.push()
        self.assertEqual(created.status_code, 200)
        self.assertEqual(created.json()["disposition"], "accepted")
        self.assertEqual(created.json()["serverVersion"], 1)

        updated = self.push(operation="update", payload={"title": "New"}, base_version=1)
        self.assertEqual(updated.json()["serverVersion"], 2)
        record = SyncRecord.objects.get(entity_id="e1")
        self.assertEqual(record.payload, {"title": "New"})
        self.assertEqual(record.updated_by, self.owner)

        deleted = self.push(operation="delete", base_version=2)
        self.assertEqual(deleted.json()["serverVersion"], 3)
        self.assertTrue(SyncRecord.objects.get(entity_id="e1").deleted)

    def test_chained_offline_edits_without_a_base_version_are_accepted(self):
        self.push()
        response = self.push(operation="update", payload={"title": "Edited offline"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["serverVersion"], 2)

    # -- conflicts ----------------------------------------------------------

    def test_update_from_a_stale_version_is_a_conflict_and_changes_nothing(self):
        self.push()
        self.push(operation="update", payload={"title": "Device A"}, base_version=1)
        stale = self.push(operation="update", payload={"title": "Device B"}, base_version=1)
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["disposition"], "conflict")
        self.assertEqual(stale.json()["serverVersion"], 2)
        self.assertEqual(SyncRecord.objects.get(entity_id="e1").payload, {"title": "Device A"})

    def test_creating_an_existing_record_is_a_conflict(self):
        self.push()
        again = self.push(mutation_id="different")
        self.assertEqual(again.status_code, 409)
        self.assertEqual(again.json()["serverVersion"], 1)

    def test_editing_a_deleted_record_is_a_conflict(self):
        self.push()
        self.push(operation="delete")
        response = self.push(operation="update", payload={"title": "Zombie"})
        self.assertEqual(response.status_code, 409)

    def test_updating_or_deleting_a_missing_record_is_rejected(self):
        self.assertEqual(self.push(operation="update", entity_id="nope", payload={"a": 1}).status_code, 422)
        self.assertEqual(self.push(operation="delete", entity_id="nope").status_code, 422)

    # -- retries ------------------------------------------------------------

    def test_a_retried_mutation_is_applied_once_and_gets_the_same_answer(self):
        first = self.push(mutation_id="same")
        retry = self.push(mutation_id="same", payload={"title": "Changed on retry"})
        self.assertEqual(first.json(), retry.json())
        self.assertEqual(SyncRecord.objects.count(), 1)
        self.assertEqual(SyncRecord.objects.get().payload, {"title": "Assembly"})
        self.assertEqual(MutationLog.objects.count(), 1)

    def test_a_retried_conflict_stays_a_conflict(self):
        self.push()
        first = self.push(mutation_id="dup", entity_id="e1")
        retry = self.push(mutation_id="dup", entity_id="e1")
        self.assertEqual((first.status_code, retry.status_code), (409, 409))

    def test_a_mutation_id_cannot_be_reused_by_someone_else(self):
        self.push(mutation_id="shared")
        self.client.force_authenticate(self.teacher_user)
        response = self.push(membership=self.teacher, mutation_id="shared", entity_id="e2")
        self.assertEqual(response.status_code, 422)
        self.assertFalse(SyncRecord.objects.filter(entity_id="e2").exists())

    # -- identity and tenancy ----------------------------------------------

    def test_a_token_is_required(self):
        self.client.force_authenticate(None)
        self.assertEqual(self.push().status_code, 401)

    def test_someone_else_membership_id_is_refused(self):
        self.client.force_authenticate(self.teacher_user)
        response = self.push(membership=self.owner)  # the owner's membership id
        self.assertEqual(response.status_code, 403)
        self.assertEqual(SyncRecord.objects.count(), 0)

    def test_a_school_the_person_does_not_belong_to_is_refused(self):
        response = self.push(school=self.other_school)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(SyncRecord.objects.count(), 0)

    def test_inactive_membership_or_school_is_refused(self):
        self.owner.is_active = False
        self.owner.save()
        self.assertEqual(self.push().status_code, 403)
        self.owner.is_active = True
        self.owner.save()
        self.school.is_active = False
        self.school.save()
        self.assertEqual(self.push().status_code, 403)

    def test_the_same_entity_id_in_two_schools_never_mixes(self):
        other_user = User.objects.create_user("owner@b.ng", "a-long-test-password-1")
        other_owner = Membership.objects.create(user=other_user, school=self.other_school, role=Role.PROPRIETOR)
        self.push(payload={"title": "School A"})
        self.client.force_authenticate(other_user)
        response = self.push(membership=other_owner, school=self.other_school, payload={"title": "School B"})
        self.assertEqual(response.json()["serverVersion"], 1)
        self.assertEqual(SyncRecord.objects.get(school=self.school).payload, {"title": "School A"})
        self.assertEqual(SyncRecord.objects.get(school=self.other_school).payload, {"title": "School B"})

    # -- record types with no handler ---------------------------------------

    @override_settings(SYNC_ALLOW_UNLISTED_ENTITY_TYPES=False)
    def test_unlisted_types_are_refused_when_unlisted_types_are_off(self):
        response = self.push(entity_type="concession_request", entity_id="p1")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(SyncRecord.objects.count(), 0)

    @override_settings(SYNC_ALLOW_UNLISTED_ENTITY_TYPES=True)
    def test_unlisted_types_are_allowed_only_when_switched_on(self):
        self.assertEqual(self.push(entity_type="concession_request", entity_id="p1").status_code, 200)

    # -- validation ---------------------------------------------------------

    def test_bad_input_is_a_400_and_saves_nothing(self):
        for change in [
            {"operation": "explode"},
            {"entityType": "Bad Type!"},
            {"entityId": "../etc/passwd"},
            {"entityId": ""},
            {"tenantId": "not-a-uuid"},
            {"baseVersion": -1},
            {"payload": {}},
        ]:
            body = {**self.mutation(), **change}
            response = self.client.post(PUSH, body, format="json")
            self.assertEqual(response.status_code, 400, change)
        self.assertEqual(SyncRecord.objects.count(), 0)
        self.assertEqual(MutationLog.objects.count(), 0)

    @override_settings(SYNC_MAX_PAYLOAD_BYTES=100)
    def test_oversized_payloads_are_refused(self):
        response = self.client.post(PUSH, self.mutation(payload={"blob": "x" * 500}), format="json")
        self.assertEqual(response.status_code, 400)

    def test_nested_payloads_round_trip_unchanged(self):
        payload = {"personal": {"phone": "08031234567"}, "documents": [{"name": "CV", "status": "received"}], "n": 3}
        self.push(payload=payload)
        self.assertEqual(SyncRecord.objects.get().payload, payload)
