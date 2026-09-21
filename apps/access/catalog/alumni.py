from .base import workspace


ALUMNI = workspace(
    "alumni",
    "Alumni",
    {"alumni"},
    [
        ("dashboard", "Home", "essential", "owner-only"),
        ("profile", "My Alumni Profile", "sensitive", "owner-only"),
        ("directory", "Alumni Directory", "owner-only"),
        ("community", "Community", "owner-only"),
        ("events", "Events & Reunions", "owner-only"),
        ("mentorship", "Mentorship", "owner-only"),
        ("opportunities", "Jobs & Opportunities", "owner-only"),
        ("give-back", "Give Back", "owner-only"),
    ],
)
