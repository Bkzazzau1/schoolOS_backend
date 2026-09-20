"""Values the structure feature shares with the app. Each list mirrors the app's."""

SECTION = "academic_section"
APPOINTMENT = "leadership_appointment"
APPEARANCE = "school_appearance"

#: The one appearance record a school has.
APPEARANCE_ID = "theme"

#: The colour schemes the app offers (SchoolThemePreset ids).
THEMES = frozenset({"forest", "ocean", "violet", "rose", "amber"})

LEVELS = ("sectionHead", "deputy", "hod", "coordinator")
SECTION_HEAD = "sectionHead"
DEPUTY = "deputy"
HOD = "hod"

#: Sections and the school's look are read by everyone in the school.
#: Appointments are read by people who work there, not by parents or students.
APPOINTMENT_READERS = frozenset({"proprietor", "principal", "administrator", "accountant", "teacher", "staff"})
