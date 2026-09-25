from apps.schools.models import Role

from .base import Activity, workspace

# parent_dashboard_demo_data.dart
PARENT = workspace(
    "parent",
    "Parent",
    {"parent"},
    [
        ("dashboard", "Home", "essential"),
        ("children", "My Children", "sensitive"),
        ("progress", "Learning Progress"),
        ("weekly-learning", "Weekly Learning"),
        ("assignments", "Assignments"),
        ("attendance", "Attendance"),
        ("finance", "Finance & Payments", "sensitive"),
        ("messages", "Messages"),
        ("discussions", "School Discussions"),
        ("school-life", "School Life"),
        ("documents", "Documents & Consent", "sensitive"),
        ("ai", "Parent AI"),
        ("transferverify", "TransferVerify Case", "sensitive"),
    ],
)

# The shared modules of school life. Existing school roles keep their current
# defaults. Alumni has its own community/events surfaces and is deliberately not
# included here until the school chooses what school-life access alumni should have.
_SCHOOL_LIFE_DEFAULT_ROLES = {
    Role.PROPRIETOR.value,
    Role.ADMINISTRATOR.value,
    Role.PRINCIPAL.value,
    Role.TEACHER.value,
    Role.ACCOUNTANT.value,
    Role.PARENT.value,
    Role.STUDENT.value,
    Role.STAFF.value,
    Role.DRIVER.value,
}

SCHOOL_LIFE = workspace(
    "schoollife",
    "School life",
    _SCHOOL_LIFE_DEFAULT_ROLES,
    [
        ("community", "Community"),
        ("noticeboard", "Noticeboard"),
        ("activities", "Activities & Clubs"),
        ("events", "Events"),
        ("houses", "Houses"),
        ("gallery", "Gallery"),
        ("excursions", "Excursions"),
        ("transport", "Transport"),
        ("meals", "Meals"),
        ("boarding", "Boarding"),
        ("assembly", "Assembly"),
        ("visitors", "Visitors"),
        ("lost-found", "Lost & Found"),
        ("service", "Service Projects"),
        ("awards", "Awards"),
        ("teaching-models", "Teaching Models"),
    ],
)

# The generic dashboard used by roles with no workspace of their own (staff and
# students). Mirrors RolePermissions in app_capability.dart.
GENERAL = [
    Activity("general.dashboard", "Dashboard", "General", frozenset({"staff", "student"}), essential=True),
    Activity("general.students", "Students", "General", frozenset({"staff"})),
    Activity("general.attendance", "Attendance", "General", frozenset()),
    Activity("general.academics", "Academics", "General", frozenset({"student"})),
    Activity("general.messages", "Messages", "General", frozenset({"staff", "student"})),
]
