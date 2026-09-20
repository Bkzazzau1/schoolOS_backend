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
        ("attendance", "Attendance"),
        ("finance", "Finance & Payments", "sensitive"),
        ("messages", "Messages"),
        ("discussions", "School Discussions"),
        ("school-life", "School Life"),
        ("documents", "Documents & Consent", "sensitive"),
        ("ai", "Parent AI"),
    ],
)

# The shared modules of school life (proprietor_school_life_data.dart). Everyone
# in the school has them by default: parents, teachers and staff can take part.
# The owner can still block any of them for a role or a person.
#
# Seeing a module is separate from posting in it. Who may post what is decided by
# each module's own rules when it is built on the server.
SCHOOL_LIFE = workspace(
    "schoollife",
    "School life",
    {role.value for role in Role},
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
