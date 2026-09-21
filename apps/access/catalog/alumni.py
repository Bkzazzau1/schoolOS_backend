from .base import workspace


ALUMNI = workspace(
    "alumni",
    "Alumni",
    {"alumni"},
    [
        ("dashboard", "Home", "essential"),
        ("profile", "My Alumni Profile", "sensitive"),
        ("directory", "Alumni Directory"),
        ("community", "Community"),
        ("events", "Events & Reunions"),
        ("mentorship", "Mentorship"),
        ("opportunities", "Jobs & Opportunities"),
        ("give-back", "Give Back"),
    ],
)
