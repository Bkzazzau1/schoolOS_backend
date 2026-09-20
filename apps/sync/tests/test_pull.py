from django.db import IntegrityError, transaction
from django.test import override_settings

from apps.schools.models import Membership, Role
from apps.staff.tests.helpers import StaffTestCase
from apps.sync import records
from apps.sync.models import SchoolSequence, SyncRecord

JOB = {"recipientType": "unregistered", "name": "Ada", "email": "ada@x.ng", "role": "sectionHead",
       "duties": ["operations.security"], "section": "Secondary", "sectionId": "SEC-1", "title": "Head of Secondary", "status": "pendingActivation"}
BANK = {"bankName": "Access", "accountName": "M I", "accountNumber": "0123456789"}


class PullTestCase(StaffTestCase):
    def make(self, entity_type, entity_id, payload=None, school=None):
        return SyncRecord.objects.create(
            school=school or self.school, entity_type=entity_type, entity_id=entity_id, payload=payload or {}
        )

    def pull(self, who, since=0, limit=None, school=None, **extra):
        self.client.force_authenticate(who.user if who else None)
        query = {"school": str((school or self.school).id), "since": since, **extra}
        if limit is not None:
            query["limit"] = limit
        return self.client.get("/api/v1/sync/pull/", query)

    def ids(self, response):
        self.assertEqual(response.status_code, 200, response.json())
        return [(r["entityType"], r["entityId"]) for r in response.json()["records"]]


class ChangeNumberTests(PullTestCase):
    def test_every_change_gets_the_next_number_and_schools_count_separately(self):
        a, b = self.make("x", "1"), self.make("x", "2")
        other = self.make("x", "1", school=self.other_school)
        self.assertEqual((a.seq, b.seq, other.seq), (1, 2, 1))
        a.payload = {"changed": True}
        a.save()
        self.assertEqual(a.seq, 3)
        self.assertEqual(SchoolSequence.objects.get(school=self.school).last, 3)

    def test_changes_through_sync_and_through_the_server_are_both_numbered(self):
        self.ok(self.push("owner_job_assignment", "J1", JOB))
        record = SyncRecord.objects.get(entity_id="J1")
        self.assertGreaterEqual(record.seq, 1)
        changed = records.write(self.school, "owner_job_assignment", "J1", {**record.payload, "note": "n"})
        self.assertGreater(changed.seq, record.seq)

    def test_the_database_refuses_a_repeated_number(self):
        a, b = self.make("x", "1"), self.make("x", "2")
        with self.assertRaises(IntegrityError), transaction.atomic():
            SyncRecord.objects.filter(pk=b.pk).update(seq=a.seq)


class PagingTests(PullTestCase):
    def test_everything_then_only_what_changed(self):
        for i in range(3):
            self.make("owner_payroll_profile", f"S{i}")
        first = self.pull(self.owner).json()
        self.assertEqual((len(first["records"]), first["hasMore"], first["cursor"]), (3, False, 3))
        self.assertEqual(self.ids(self.pull(self.owner, since=first["cursor"])), [])
        self.assertEqual(self.pull(self.owner, since=3).json()["cursor"], 3)

        record = SyncRecord.objects.get(entity_id="S1")
        record.payload = {"gross": 5}
        record.version += 1
        record.save()
        later = self.pull(self.owner, since=first["cursor"]).json()
        self.assertEqual([(r["entityId"], r["version"], r["payload"]) for r in later["records"]], [("S1", 2, {"gross": 5})])

    def test_pages_are_in_order_and_join_up_exactly(self):
        for i in range(7):
            self.make("owner_payroll_profile", f"S{i}")
        seen, cursor, pages = [], 0, 0
        while True:
            page = self.pull(self.owner, since=cursor, limit=3).json()
            seen += [r["entityId"] for r in page["records"]]
            cursor, pages = page["cursor"], pages + 1
            if not page["hasMore"]:
                break
        self.assertEqual((seen, pages), ([f"S{i}" for i in range(7)], 3))

    def test_a_record_changed_twice_is_sent_once_at_its_latest(self):
        record = self.make("owner_payroll_profile", "S1", {"v": 1})
        for n in (2, 3):
            record.payload, record.version = {"v": n}, n
            record.save()
        found = self.pull(self.owner).json()["records"]
        self.assertEqual([(r["version"], r["payload"]) for r in found], [(3, {"v": 3})])

    def test_a_deleted_record_comes_back_as_a_marker_without_its_content(self):
        record = self.make("owner_payroll_profile", "S1", {"gross": 1})
        record.deleted, record.version = True, 2
        record.save()
        (only,) = self.pull(self.owner).json()["records"]
        self.assertEqual((only["deleted"], only["payload"]), (True, {}))

    def test_the_page_size_is_capped_and_bad_values_are_refused(self):
        self.make("owner_payroll_profile", "S1")
        self.assertEqual(self.pull(self.owner, limit=100000).status_code, 200)
        for bad in ({"since": -1}, {"since": "abc"}, {"limit": 0}):
            self.client.force_authenticate(self.owner.user)
            response = self.client.get("/api/v1/sync/pull/", {"school": str(self.school.id), **bad})
            self.assertEqual(response.status_code, 400, bad)

    def test_two_devices_that_start_apart_end_up_with_the_same_records(self):
        for i in range(5):
            self.make("owner_payroll_profile", f"S{i}")
        early = self.pull(self.owner, limit=2).json()
        self.make("owner_payroll_profile", "S9")
        rest = self.pull(self.owner, since=early["cursor"]).json()
        fresh = self.pull(self.owner).json()
        combined = {r["entityId"] for r in early["records"] + rest["records"]}
        self.assertEqual(combined, {r["entityId"] for r in fresh["records"]})


class WhoMayPullTests(PullTestCase):
    def test_only_a_member_of_that_school_may_pull_it(self):
        self.make("owner_payroll_profile", "S1")
        self.assertEqual(self.pull(None).status_code, 401)
        self.assertEqual(self.pull(self.other_owner).status_code, 403)
        self.assertEqual(self.pull(self.owner, school=self.other_school).status_code, 403)

    def test_a_school_never_receives_another_schools_records(self):
        self.make("owner_payroll_profile", "MINE")
        self.make("owner_payroll_profile", "THEIRS", school=self.other_school)
        self.assertEqual(self.ids(self.pull(self.owner)), [("owner_payroll_profile", "MINE")])
        theirs = self.pull(self.other_owner, school=self.other_school)
        self.assertEqual(self.ids(theirs), [("owner_payroll_profile", "THEIRS")])

    def test_someone_who_left_gets_nothing(self):
        Membership.objects.filter(id=self.owner.id).update(is_active=False)
        self.assertEqual(self.pull(self.owner).status_code, 403)

    def test_a_person_with_two_roles_must_say_which(self):
        second = Membership.objects.create(user=self.members["teacher"].user, school=self.school, role=Role.PARENT)
        response = self.pull(self.members["teacher"])
        self.assertEqual((response.status_code, response.json()["code"]), (400, "membership_required"))
        self.assertEqual(self.pull(self.members["teacher"], membership=str(second.id)).status_code, 200)
        self.assertEqual(self.pull(self.members["teacher"], membership=str(self.owner.id)).status_code, 403)


class WhatEachRoleSeesTests(PullTestCase):
    def setUp(self):
        super().setUp()
        self.staff_id = self.make_staff(email="musa@school.ng", systemRole="teacher")
        self.staff_login = self.members["staff"]
        self.link(self.staff_id, self.staff_login)

    def kinds(self, who):
        return {entity_type for entity_type, _ in self.ids(self.pull(who))}

    def bank_of(self, who):
        found = [r for r in self.pull(who).json()["records"] if r["entityType"] == "owner_staff_profile"]
        return found[0]["payload"]["payment"] if found else None

    def test_salaries_go_to_the_owner_and_finance_officers_only(self):
        for role in ("proprietor", "accountant"):
            self.assertIn("owner_payroll_profile", self.kinds(self.members[role]), role)
        for role in ("principal", "administrator", "teacher", "staff", "parent", "student"):
            self.assertNotIn("owner_payroll_profile", self.kinds(self.members[role]), role)

    def test_the_staff_list_goes_to_those_who_run_the_school(self):
        for role in ("proprietor", "principal", "administrator", "accountant"):
            self.assertIn("administrator_staff_directory", self.kinds(self.members[role]), role)
        for role in ("teacher", "parent", "student", "staff"):
            self.assertNotIn("administrator_staff_directory", self.kinds(self.members[role]), role)

    def test_bank_details_reach_the_owner_the_principal_and_the_person_only(self):
        record = SyncRecord.objects.get(entity_type="owner_staff_profile", entity_id=self.staff_id)
        record.payload = {**record.payload, "payment": BANK}
        record.save()
        for who in (self.owner, self.members["principal"], self.staff_login):
            self.assertEqual(self.bank_of(who)["accountNumber"], "0123456789", who.role)
        self.assertEqual(self.bank_of(self.members["administrator"]), {"bankName": "", "accountName": "", "accountNumber": ""})
        for role in ("teacher", "accountant", "parent", "student"):
            self.assertIsNone(self.bank_of(self.members[role]), role)

    def test_hiding_bank_details_from_one_person_changes_nothing_stored(self):
        record = SyncRecord.objects.get(entity_type="owner_staff_profile")
        record.payload = {**record.payload, "payment": BANK}
        record.save()
        self.bank_of(self.members["administrator"])
        self.assertEqual(self.bank_of(self.owner)["accountNumber"], "0123456789")
        self.assertEqual(SyncRecord.objects.get(pk=record.pk).payload["payment"]["accountNumber"], "0123456789")

    def proposals(self, who):
        return [i for t, i in self.ids(self.pull(who)) if t == "staff_proposal"]

    def test_a_proposal_is_seen_by_its_proposer_the_owner_and_assigned_approvers_only(self):
        self.ok(self.propose(who=self.members["principal"], proposal_id="P1", email="new@school.ng"))
        self.assign_approver(self.members["administrator"])
        for who in (self.owner, self.members["principal"], self.members["administrator"]):
            self.assertIn("P1", self.proposals(who), who.role)
        for role in ("accountant", "teacher", "staff", "parent"):
            self.assertNotIn("P1", self.proposals(self.members[role]), role)

    def test_an_unclaimed_approver_assignment_shows_nothing_extra(self):
        self.ok(self.propose(who=self.members["principal"], proposal_id="P1", email="new@school.ng"))
        self.assign_approver(self.members["accountant"], status="pendingActivation")
        self.assertNotIn("P1", self.proposals(self.members["accountant"]))

    def test_a_person_sees_their_own_authority_and_jobs_not_others(self):
        me, other = str(self.members["accountant"].id), str(self.members["teacher"].id)
        self.make("owner_payroll_authorizer", "A-ME", {"membershipId": me, "status": "active"})
        self.make("owner_payroll_authorizer", "A-OTHER", {"membershipId": other, "status": "active"})
        self.make("owner_job_assignment", "J-ME", {"membershipId": me})
        mine = [i for t, i in self.ids(self.pull(self.members["accountant"]))
                if t in ("owner_payroll_authorizer", "owner_job_assignment")]
        self.assertEqual(sorted(mine), ["A-ME", "J-ME"])

    def test_the_cursor_moves_past_records_the_person_may_not_see(self):
        page = self.pull(self.members["teacher"]).json()
        self.assertEqual(page["records"], [])
        self.assertEqual(page["cursor"], SchoolSequence.objects.get(school=self.school).last)

    def test_kinds_without_rules_are_only_for_the_owner_and_only_in_development(self):
        self.make("some_new_thing", "1", {"a": 1})
        with override_settings(SYNC_ALLOW_UNLISTED_ENTITY_TYPES=True):
            self.assertIn("some_new_thing", self.kinds(self.owner))
            self.assertNotIn("some_new_thing", self.kinds(self.members["principal"]))
        with override_settings(SYNC_ALLOW_UNLISTED_ENTITY_TYPES=False):
            self.assertNotIn("some_new_thing", self.kinds(self.owner))

    def test_a_change_pushed_from_one_device_reaches_another(self):
        before = self.pull(self.owner).json()["cursor"]
        self.ok(self.push("owner_job_assignment", "J9", JOB))
        news = self.pull(self.owner, since=before).json()["records"]
        self.assertEqual([r["entityId"] for r in news], ["J9"])
