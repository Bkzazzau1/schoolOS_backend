"""The rules that are particular to one module."""

from apps.schoollife.specs import calendar, campus, communications, programmes

from .test_modules import ModuleTestCase, sample


class ContributorTests(ModuleTestCase):
    def test_contributors_add_records_and_change_only_their_own(self):
        spec = calendar.EVENTS
        teacher, other_teacher = self.members["teacher"], self.members["administrator"]
        self.ok(self.send(spec, teacher, "mine"))
        self.ok(self.push(spec.entity_type, "mine", sample(spec, "mine", note="changed"), operation="update", who=teacher))
        self.assertEqual(self.stored_of(spec, "mine")["note"], "changed")
        # A record added by a manager is not theirs to change.
        self.ok(self.send(spec, other_teacher, "theirs"))
        self.rejected(self.push(spec.entity_type, "theirs", sample(spec, "theirs", note="x"), operation="update", who=teacher), "added yourself")
        # And they cannot take over a record by claiming it.
        self.rejected(self.push(spec.entity_type, "theirs", sample(spec, "theirs", createdByMembershipId=str(teacher.id)),
                                operation="update", who=teacher), "added yourself")
        self.assertEqual(self.stored_of(spec, "theirs")["createdByMembershipId"], str(other_teacher.id))

    def test_a_contributor_cannot_delete_even_their_own(self):
        spec = calendar.EVENTS
        self.ok(self.send(spec, self.members["teacher"], "mine"))
        self.rejected(self.push(spec.entity_type, "mine", operation="delete", who=self.members["teacher"]), "cannot be deleted")

    def test_the_owner_of_a_record_stays_the_original_author_when_a_manager_edits_it(self):
        spec = calendar.EVENTS
        teacher = self.members["teacher"]
        self.ok(self.send(spec, teacher, "mine"))
        self.ok(self.push(spec.entity_type, "mine", sample(spec, "mine", note="tidied"), operation="update", who=self.owner))
        p = self.stored_of(spec, "mine")
        self.assertEqual((p["createdByMembershipId"], p["updatedByMembershipId"]), (str(teacher.id), str(self.owner.id)))


class GuardedFieldTests(ModuleTestCase):
    def test_review_ticks_are_the_leaderships_to_give(self):
        for spec, field in ((calendar.EXCURSIONS, "readinessReviewed"), (programmes.SERVICE, "verified"),
                            (campus.TRANSPORT, "reviewed"), (campus.BOARDING, "handoverReviewed")):
            admin, principal = self.members["administrator"], self.members["principal"]
            self.ok(self.send(spec, admin, "r1"))
            self.assertIs(self.stored_of(spec, "r1")[field], False)
            self.rejected(self.push(spec.entity_type, "r1", sample(spec, "r1", **{field: True}), operation="update", who=admin),
                          f"can change {field}")
            self.ok(self.push(spec.entity_type, "r1", sample(spec, "r1", **{field: True}), operation="update", who=principal))
            self.assertIs(self.stored_of(spec, "r1")[field], True)
            # An edit that does not touch the tick keeps it.
            payload = {k: v for k, v in sample(spec, "r1", note="later").items() if k != field}
            self.ok(self.push(spec.entity_type, "r1", payload, operation="update", who=admin))
            self.assertIs(self.stored_of(spec, "r1")[field], True)

    def test_a_new_record_cannot_arrive_already_reviewed(self):
        spec = calendar.EXCURSIONS
        self.rejected(self.send(spec, self.members["administrator"], payload=sample(spec, readinessReviewed=True)), "readinessReviewed")
        self.ok(self.send(spec, self.owner, "ok-1", payload=sample(spec, "ok-1", readinessReviewed=True)))

    def test_the_front_desk_review_belongs_to_the_managers(self):
        spec = campus.VISITORS
        self.ok(self.send(spec, self.members["staff"], "v1"))
        self.rejected(self.push(spec.entity_type, "v1", sample(spec, "v1", frontDeskReviewed=True), operation="update", who=self.members["staff"]),
                      "frontDeskReviewed")
        self.ok(self.push(spec.entity_type, "v1", sample(spec, "v1", frontDeskReviewed=True), operation="update", who=self.members["administrator"]))

    def test_pinning_a_notice_is_for_the_owner_and_principal_and_counters_are_the_servers(self):
        spec = communications.NOTICEBOARD
        admin = self.members["administrator"]
        self.ok(self.send(spec, admin, "n1", payload=sample(spec, "n1", readCount=921, totalRecipients=1084)))
        p = self.stored_of(spec, "n1")
        self.assertEqual((p["readCount"], p["totalRecipients"], p["pinned"]), (0, 0, False))
        self.rejected(self.push(spec.entity_type, "n1", sample(spec, "n1", pinned=True), operation="update", who=admin), "pinned")
        self.ok(self.push(spec.entity_type, "n1", sample(spec, "n1", pinned=True, readCount=5000), operation="update", who=self.members["principal"]))
        p = self.stored_of(spec, "n1")
        self.assertEqual((p["pinned"], p["readCount"]), (True, 0))

    def test_the_public_showcase_is_for_the_owner_and_principal(self):
        for spec in (campus.GALLERY, programmes.AWARDS):
            teacher = self.members["teacher"]
            self.ok(self.send(spec, teacher, "g1", payload=sample(spec, "g1", visibility="internal" if spec is campus.GALLERY else "schoolAndParents")))
            self.rejected(self.push(spec.entity_type, "g1", sample(spec, "g1", visibility="publicShowcase"), operation="update", who=teacher),
                          "publicShowcase")
            self.rejected(self.send(spec, teacher, "g2", payload=sample(spec, "g2", visibility="publicShowcase")), "publicShowcase")
            self.ok(self.push(spec.entity_type, "g1", sample(spec, "g1", visibility="publicShowcase"), operation="update", who=self.members["principal"]))
            # Already public: an unrelated edit by a manager does not need the leadership again.
            self.ok(self.push(spec.entity_type, "g1", sample(spec, "g1", visibility="publicShowcase", note="typo"), operation="update",
                              who=self.members["administrator"] if spec is campus.GALLERY else self.owner))

    def test_only_office_staff_settle_a_lost_item_claim(self):
        spec = campus.LOST_FOUND
        parent = self.members["parent"]
        self.ok(self.send(spec, parent, "l1", payload=sample(spec, "l1", status="Awaiting claim")))     # anyone can report a found item
        self.rejected(self.push(spec.entity_type, "l1", sample(spec, "l1", status="Returned", claimant="Me"), operation="update", who=parent), "can change")
        self.ok(self.push(spec.entity_type, "l1", sample(spec, "l1", status="Returned", claimant="Aisha"), operation="update", who=self.members["staff"]))
        self.assertEqual(self.stored_of(spec, "l1")["status"], "Returned")


class WhoReadsWhatTests(ModuleTestCase):
    def kinds(self, who):
        return {t for t, _ in self.pulled(who)}

    def test_visitor_boarding_and_teaching_records_stay_on_the_staff_side(self):
        for spec in (campus.VISITORS, campus.BOARDING, programmes.TEACHING_MODELS):
            manager = self.members["principal"]
            self.ok(self.send(spec, manager, "s1"))
            for role in ("proprietor", "principal", "administrator", "accountant", "teacher", "staff"):
                self.assertIn(spec.entity_type, self.kinds(self.members[role]), (spec.entity_type, role))
            for role in ("parent", "student"):
                self.assertNotIn(spec.entity_type, self.kinds(self.members[role]), (spec.entity_type, role))

    def test_open_modules_reach_everyone_including_families(self):
        for spec in (calendar.EVENTS, calendar.ASSEMBLY, campus.TRANSPORT, campus.MEALS, programmes.HOUSES, programmes.ACTIVITIES):
            self.ok(self.send(spec, self.owner, "o1"))
            for role in ("teacher", "parent", "student"):
                self.assertIn(spec.entity_type, self.kinds(self.members[role]), (spec.entity_type, role))

    def test_internal_pictures_and_awards_are_not_for_families(self):
        gallery, award = campus.GALLERY, programmes.AWARDS
        self.ok(self.send(gallery, self.owner, "g1", payload=sample(gallery, "g1", visibility="internal")))
        self.ok(self.send(gallery, self.owner, "g2", payload=sample(gallery, "g2", visibility="parents")))
        self.ok(self.send(award, self.owner, "a1", payload=sample(award, "a1", visibility="internalOnly")))
        self.ok(self.send(award, self.owner, "a2", payload=sample(award, "a2", visibility="schoolAndParents")))
        parent = self.pulled(self.members["parent"])
        self.assertEqual({i for t, i in parent if t in ("gallery_media_album", "award_recognition")}, {"g2", "a2"})
        teacher = self.pulled(self.members["teacher"])
        self.assertEqual({i for t, i in teacher if t in ("gallery_media_album", "award_recognition")}, {"g1", "g2", "a1", "a2"})

    def test_a_staff_notice_is_for_staff_and_a_contributor_always_sees_their_own(self):
        notice = communications.NOTICEBOARD
        self.ok(self.send(notice, self.owner, "n1", payload=sample(notice, "n1", audience="staffOnly")))
        self.ok(self.send(notice, self.owner, "n2", payload=sample(notice, "n2", audience="wholeSchool")))
        self.assertEqual({i for t, i in self.pulled(self.members["parent"]) if t == "noticeboard_notice"}, {"n2"})
        self.assertEqual({i for t, i in self.pulled(self.members["teacher"]) if t == "noticeboard_notice"}, {"n1", "n2"})
        # A record a contributor added stays visible to them even if the module is otherwise narrower.
        visitors = campus.VISITORS
        self.ok(self.send(visitors, self.members["staff"], "v1"))
        self.assertIn(("visitor_record", "v1"), self.pulled(self.members["staff"]))
