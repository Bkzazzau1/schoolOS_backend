"""Alumni Events & Reunions: school management creates real events, a real Alumni membership
browses and RSVPs - never proposes their own event, never a fabricated attendee count."""

from apps.staff.tests.helpers import StaffTestCase

from ..models import AlumniEvent, AlumniEventRsvp


class AlumniEventTests(StaffTestCase):
    def _event(self, **over):
        defaults = dict(school=self.school, title="Class of 2015 Reunion", date="2026-12-12", time_text="4:00 PM")
        defaults.update(over)
        return AlumniEvent.objects.create(**defaults)

    def _get(self, who):
        self.client.force_authenticate(who.user)
        return self.client.get(f"/api/v1/alumni/schools/{self.school.id}/events/")

    def _create(self, who, **body):
        self.client.force_authenticate(who.user)
        payload = {"title": "Class of 2015 Reunion", "date": "2026-12-12", "timeText": "4:00 PM"}
        payload.update(body)
        return self.client.post(f"/api/v1/alumni/schools/{self.school.id}/events/", payload, format="json")

    def _rsvp(self, who, event, attending):
        self.client.force_authenticate(who.user)
        return self.client.post(
            f"/api/v1/alumni/schools/{self.school.id}/events/{event.id}/rsvp/",
            {"attending": attending},
            format="json",
        )

    def test_only_real_alumni_can_browse_events(self):
        self._event()
        response = self._get(self.members["alumni"])
        self.assertEqual(response.status_code, 200, response.json())
        self.assertEqual(len(response.json()["events"]), 1)
        for role in ("teacher", "parent", "proprietor", "administrator", "principal", "student"):
            self.assertEqual(self._get(self.members[role]).status_code, 403, role)

    def test_only_management_can_create_an_event(self):
        for role in ("proprietor", "administrator", "principal"):
            response = self._create(self.members[role], title=f"Event by {role}")
            self.assertEqual(response.status_code, 201, (role, response.json()))
        for role in ("alumni", "teacher", "parent"):
            self.assertEqual(self._create(self.members[role]).status_code, 403, role)

    def test_an_event_is_refused_without_a_real_title_or_date(self):
        response = self._create(self.members["proprietor"], title="")
        self.assertEqual(response.status_code, 400)
        response = self._create(self.members["proprietor"], date="not-a-date")
        self.assertEqual(response.status_code, 400)

    def test_an_alumnus_can_rsvp_and_the_count_is_real(self):
        event = self._event()
        second_user_response = self._create(self.members["proprietor"])
        self.assertEqual(second_user_response.status_code, 201)

        response = self._rsvp(self.members["alumni"], event, True)
        self.assertEqual(response.status_code, 200, response.json())
        self.assertEqual(response.json()["event"]["attendingCount"], 1)
        self.assertIs(response.json()["event"]["myRsvp"], True)

    def test_changing_an_rsvp_updates_the_same_row_rather_than_duplicating(self):
        event = self._event()
        self._rsvp(self.members["alumni"], event, True)
        self.assertEqual(AlumniEventRsvp.objects.filter(event=event).count(), 1)
        response = self._rsvp(self.members["alumni"], event, False)
        self.assertEqual(AlumniEventRsvp.objects.filter(event=event).count(), 1)
        self.assertEqual(response.json()["event"]["attendingCount"], 0)
        self.assertIs(response.json()["event"]["myRsvp"], False)

    def test_an_event_with_nobody_responding_has_an_honest_null_rsvp_and_zero_count(self):
        event = self._event()
        response = self._get(self.members["alumni"])
        entry = response.json()["events"][0]
        self.assertEqual(entry["attendingCount"], 0)
        self.assertIsNone(entry["myRsvp"])

    def test_a_non_alumnus_cannot_rsvp(self):
        event = self._event()
        response = self._rsvp(self.members["teacher"], event, True)
        self.assertEqual(response.status_code, 403)

    def test_rsvp_to_a_forged_or_other_schools_event_id_is_refused(self):
        self.client.force_authenticate(self.members["alumni"].user)
        ghost_id = "00000000-0000-0000-0000-000000000000"
        response = self.client.post(
            f"/api/v1/alumni/schools/{self.school.id}/events/{ghost_id}/rsvp/", {"attending": True}, format="json"
        )
        self.assertEqual(response.status_code, 404)

        other_event = AlumniEvent.objects.create(school=self.other_school, title="Other School Reunion", date="2026-01-01")
        self.assertEqual(self._rsvp(self.members["alumni"], other_event, True).status_code, 404)

    def test_cross_school_events_never_appear(self):
        AlumniEvent.objects.create(school=self.other_school, title="Other School Reunion", date="2026-01-01")
        self._event()
        response = self._get(self.members["alumni"])
        self.assertEqual(len(response.json()["events"]), 1)
