from apps.sync.models import SyncRecord

from .helpers import OwnerTestCase


def grant(authorities=("view", "approve"), status="pendingActivation", **extra):
    payload = {
        "staffId": "STAFF-009", "name": "Mrs. Khadija Musa",
        "authorities": list(authorities), "status": status,
    }
    payload.update(extra)
    return payload


class AuthorizerTests(OwnerTestCase):
    entity_type = "owner_payroll_authorizer"

    def link_account(self, membership):
        """What account activation will do on the server."""
        record = self.stored("STAFF-009")
        record.payload = {**record.payload, "status": "active", "membershipId": str(membership.id)}
        record.save()

    def test_the_owner_grants_authority_and_the_server_records_it(self):
        self.assertAccepted(self.push("STAFF-009", grant(("view", "approve", "view"))))
        stored = self.stored("STAFF-009").payload
        self.assertEqual(stored["authorities"], ["approve", "view"])  # de-duplicated, sorted
        self.assertEqual(stored["status"], "pendingActivation")
        self.assertIsNone(stored["membershipId"])
        self.assertEqual(stored["grantedByMembershipId"], str(self.owner.id))

    def test_nobody_but_the_owner_may_grant_authority(self):
        for role, member in self.members.items():
            if role != "proprietor":
                self.assertRejected(self.push("STAFF-009", grant(), who=member), "role")
        self.assertEqual(SyncRecord.objects.count(), 0)

    def test_every_authority_the_app_offers_is_accepted(self):
        every = ("prepare", "approve", "pay", "approveStaff", "view")
        self.assertAccepted(self.push("STAFF-009", grant(every)))

    def test_unknown_or_missing_authority_is_refused(self):
        self.assertRejected(self.push("STAFF-009", grant(("view", "root"))), "root")
        self.assertRejected(self.push("STAFF-009", grant(())), "at least")
        self.assertRejected(self.push("STAFF-009", {**grant(), "authorities": "view"}), "list")
        self.assertRejected(self.push("STAFF-009", grant(authorities=[1, 2])), "list")

    def test_the_app_cannot_make_a_grant_active_or_link_an_account(self):
        self.assertRejected(self.push("STAFF-009", grant(status="active")), "activating")
        self.assertRejected(self.push("STAFF-009", grant(status="bogus")), "status")
        # A membership id sent by the app is ignored, not stored.
        someone = str(self.members["teacher"].id)
        self.assertAccepted(self.push("STAFF-009", grant(membershipId=someone)))
        self.assertIsNone(self.stored("STAFF-009").payload["membershipId"])
        # And it cannot be smuggled in later either.
        self.assertRejected(self.push("STAFF-009", grant(status="active", membershipId=someone), operation="update"))
        self.assertIsNone(self.stored("STAFF-009").payload["membershipId"])

    def test_editing_an_active_grant_keeps_it_active_and_linked(self):
        teacher = self.members["teacher"]
        self.push("STAFF-009", grant())
        self.link_account(teacher)
        # The app echoes what it last saw, but with a different membership id.
        response = self.push(
            "STAFF-009",
            grant(("view", "pay"), status="active", membershipId=str(self.members["staff"].id)),
            operation="update",
        )
        self.assertAccepted(response)
        stored = self.stored("STAFF-009").payload
        self.assertEqual((stored["status"], stored["membershipId"]), ("active", str(teacher.id)))
        self.assertEqual(stored["authorities"], ["pay", "view"])

    def test_revoking_keeps_the_record_and_who_it_was_linked_to(self):
        teacher = self.members["teacher"]
        self.push("STAFF-009", grant())
        self.link_account(teacher)
        self.assertAccepted(self.push("STAFF-009", grant(status="revoked"), operation="update"))
        stored = self.stored("STAFF-009").payload
        self.assertEqual(stored["status"], "revoked")
        self.assertEqual(stored["membershipId"], str(teacher.id))
        self.assertEqual(stored["revokedByMembershipId"], str(self.owner.id))
        # A revoked grant cannot be brought back by claiming it is active.
        self.assertRejected(self.push("STAFF-009", grant(status="active"), operation="update"), "activating")

    def test_a_grant_cannot_be_deleted_only_revoked(self):
        self.push("STAFF-009", grant())
        self.assertRejected(self.push("STAFF-009", operation="delete"), "deleted")
        self.assertFalse(self.stored("STAFF-009").deleted)

    def test_identity_is_checked(self):
        self.assertRejected(self.push("STAFF-010", grant()), "staffId")
        self.assertRejected(self.push("STAFF-009", grant(name=" ")), "name")
