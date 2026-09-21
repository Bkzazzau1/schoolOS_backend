import re

from django.test import SimpleTestCase

from apps.access import catalog

# The screen keys the Flutter app uses today. If the app adds, renames or removes
# a screen, update the catalog and this list together.
APP_SCREENS = {
    "owner": [
        "overview", "finance", "enrollment", "staff", "jobs", "staff-profiles", "payroll", "reports",
        "campuses", "ai", "structure", "appearance", "school-life",
        "finance-approvals",  # a screen inside Owner Finance
    ],
    "principal": [
        "dashboard", "teachers", "staff-profiles", "assignments", "academics", "students", "attendance",
        "approvals", "results", "timetable", "communication", "incidents", "ai", "performance", "profile",
    ],
    "administrator": [
        "dashboard", "admissions", "website", "registration", "students", "staff", "staff-profiles",
        "staff-attendance", "records", "lifecycle", "attendance", "operations", "notices",
    ],
    "finance": [
        "dashboard", "fee-structure", "scholarships", "collections", "reminders", "store", "mandates",
        "debt-aging", "receipts", "accounts", "reconciliation", "expenses", "payroll", "reports", "ai",
    ],
    "driver": [
        "dashboard", "morning", "afternoon", "riders", "route", "vehicle-check", "incidents", "messages", "history",
    ],
    "teacher": [
        "dashboard", "timetable", "classes", "attendance", "lesson-plans", "weekly-progress", "syllabus",
        "assignments", "assessments", "cbt", "learning-progress", "students", "messages", "ai",
        "performance", "profile",
    ],
    "parent": [
        "dashboard", "children", "progress", "weekly-learning", "attendance", "finance", "messages",
        "discussions", "school-life", "documents", "ai",
    ],
    "schoollife": [
        "community", "noticeboard", "activities", "events", "houses", "gallery", "excursions", "transport",
        "meals", "boarding", "assembly", "visitors", "lost-found", "service", "awards", "teaching-models",
    ],
    "general": ["dashboard", "students", "attendance", "academics", "messages"],
}


def keys(workspace):
    return {k for k in catalog.ACTIVITIES if k.startswith(workspace + ".")}


class CatalogShapeTests(SimpleTestCase):
    def test_keys_are_unique_well_formed_and_labelled(self):
        for key, activity in catalog.ACTIVITIES.items():
            self.assertEqual(key, activity.key)
            self.assertRegex(key, r"^[a-z]+\.[a-z0-9-]+$")
            self.assertTrue(activity.label.strip(), key)
            self.assertTrue(activity.area, key)

    def test_every_screen_in_the_app_is_an_activity_and_nothing_else_is(self):
        for workspace, screens in APP_SCREENS.items():
            expected = {f"{workspace}.{s}" for s in screens}
            if workspace == "owner":
                expected.add("owner.access")  # new: the screen for managing this feature
            self.assertEqual(keys(workspace), expected, workspace)
        self.assertEqual(len(catalog.ACTIVITIES), 115)

    def test_every_workspace_has_a_landing_screen_that_cannot_be_removed(self):
        for workspace in ["owner", "principal", "administrator", "finance", "teacher", "driver", "parent", "general"]:
            landing = [a for a in catalog.ACTIVITIES.values() if a.key.startswith(workspace + ".") and a.essential]
            self.assertEqual(len(landing), 1, workspace)

    def test_only_the_screen_that_manages_access_is_owner_only(self):
        self.assertEqual({k for k, a in catalog.ACTIVITIES.items() if not a.grantable}, {"owner.access"})

    def test_screens_that_show_money_or_personal_data_are_marked_sensitive(self):
        for key in ["owner.payroll", "owner.finance", "owner.staff-profiles", "finance.payroll",
                    "finance.accounts", "administrator.records", "parent.finance", "parent.children"]:
            self.assertTrue(catalog.get(key).sensitive, key)


class DefaultsByRoleTests(SimpleTestCase):
    def test_each_role_gets_its_own_workspace_plus_school_life(self):
        for role, workspace in [("principal", "principal"), ("administrator", "administrator"),
                                ("accountant", "finance"), ("teacher", "teacher"), ("parent", "parent")]:
            self.assertEqual(catalog.default_keys(role), keys(workspace) | keys("schoollife"), role)

    def test_the_owner_has_the_owner_workspace_and_school_life(self):
        self.assertEqual(catalog.default_keys("proprietor"), keys("owner") | keys("schoollife"))

    def test_staff_and_students_get_the_generic_dashboard_like_the_app_does_plus_school_life(self):
        self.assertEqual(
            catalog.default_keys("staff"),
            {"general.dashboard", "general.students", "general.messages"} | keys("schoollife"),
        )
        self.assertEqual(
            catalog.default_keys("student"),
            {"general.dashboard", "general.academics", "general.messages"} | keys("schoollife"),
        )

    def test_no_role_gets_another_roles_workspace_by_default(self):
        others = {"owner", "principal", "administrator", "finance", "teacher", "parent", "general"}
        own = {"principal": "principal", "administrator": "administrator", "accountant": "finance",
               "teacher": "teacher", "parent": "parent", "staff": "general", "student": "general"}
        for role, workspace in own.items():
            defaults = catalog.default_keys(role)
            for other in others - {workspace}:
                self.assertFalse(defaults & keys(other), f"{role} should not default to {other}")

    def test_school_life_is_for_everyone_and_owner_screens_are_for_the_owner_only(self):
        every_role = ["proprietor", "administrator", "principal", "teacher", "accountant", "parent", "student", "staff"]
        for role in every_role:
            self.assertTrue(keys("schoollife") <= catalog.default_keys(role), role)
            if role != "proprietor":
                self.assertFalse(catalog.default_keys(role) & keys("owner"), role)

    def test_essential_keys_are_each_roles_landing_screen(self):
        self.assertEqual(catalog.essential_keys("principal"), {"principal.dashboard"})
        self.assertEqual(catalog.essential_keys("accountant"), {"finance.dashboard"})
        self.assertEqual(catalog.essential_keys("parent"), {"parent.dashboard"})
        self.assertEqual(catalog.essential_keys("staff"), {"general.dashboard"})
        self.assertEqual(catalog.essential_keys("proprietor"), {"owner.overview"})
