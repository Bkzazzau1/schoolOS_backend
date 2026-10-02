from .base import workspace

DRIVER = workspace(
    "driver",
    "Driver",
    {"driver"},
    [
        ("dashboard", "Dashboard", "essential"),
        ("morning", "Morning Run"),
        ("afternoon", "Afternoon Run"),
        ("riders", "Riders"),
        ("route", "Route & Stops"),
        ("vehicle-check", "Vehicle Check"),
        ("incidents", "Incidents"),
        ("messages", "Messages & Alerts"),
        ("history", "Trip History & Profile"),
        ("community", "Community"),
        # Lets a holder of a job-assignment duty (e.g. preparing a direct-debit batch) reach the
        # real hub that duty unlocks, even though it lives outside the Driver workspace. Empty of
        # content for anyone the owner has not actually given such a duty to.
        ("my-duties", "My Duties", "sensitive"),
    ],
)

# Screen keys match the navigation lists in the app:
#   principal_dashboard_demo_data.dart, administrator_dashboard_demo_data.dart,
#   finance_office_dashboard_demo_data.dart, teacher_dashboard_demo_data.dart.

PRINCIPAL = workspace(
    "principal",
    "Principal",
    {"principal"},
    [
        ("dashboard", "Dashboard", "essential"),
        ("teachers", "Teachers", "sensitive"),
        ("staff-profiles", "Staff Profiles", "sensitive"),
        ("assignments", "Teaching Assignments"),
        ("class-teachers", "Class Teachers"),
        ("academics", "Academics"),
        ("students", "Students"),
        ("attendance", "Attendance"),
        ("approvals", "Approvals"),
        ("results", "Results & Reports"),
        ("timetable", "Timetable"),
        ("excursions", "Excursions"),
        ("gallery", "Media Gallery"),
        ("alumni", "Alumni Management", "sensitive"),
        ("communication", "Communication"),
        ("incidents", "Incidents"),
        ("ai", "Principal AI"),
        ("performance", "School Performance"),
        ("profile", "Profile"),
        ("community", "Community"),
        ("my-duties", "My Duties", "sensitive"),
    ],
)

ADMINISTRATOR = workspace(
    "administrator",
    "Administrator",
    {"administrator"},
    [
        ("dashboard", "Dashboard", "essential"),
        ("admissions", "Admissions Pipeline"),
        ("website", "Website Manager"),
        ("registration", "Student Registration"),
        ("students", "Students & Families"),
        ("academics", "Academic Structure"),
        ("curriculum", "Subjects & Curriculum"),
        ("timetable", "Timetable"),
        ("alumni", "Alumni Management", "sensitive"),
        ("staff", "Staff Records"),
        ("staff-profiles", "Staff Profiles", "sensitive"),
        ("staff-attendance", "Staff Attendance"),
        ("records", "Records & Documents", "sensitive"),
        ("lifecycle", "Transfers & Promotion"),
        ("attendance", "Attendance Desk"),
        ("operations", "Operations"),
        ("notices", "Notices"),
        ("community", "Community"),
        ("my-duties", "My Duties", "sensitive"),
    ],
)

FINANCE = workspace(
    "finance",
    "Finance office",
    {"accountant"},
    [
        ("dashboard", "Dashboard", "essential"),
        ("fee-structure", "Fee Structure"),
        ("scholarships", "Scholarships & Discounts"),
        ("collections", "Smart Money Collection", "sensitive"),
        ("reminders", "Fee Reminders"),
        ("store", "School Store"),
        ("mandates", "Mandates & Direct Debit", "sensitive"),
        ("debt-aging", "Outstanding & Aging"),
        ("receipts", "Receipts"),
        ("accounts", "Student Accounts", "sensitive"),
        ("reconciliation", "Reconciliation", "sensitive"),
        ("expenses", "Expenses & Income", "sensitive"),
        ("payroll", "Payroll Handoff", "sensitive"),
        ("reports", "Reports"),
        ("ai", "Finance AI"),
        ("community", "Community"),
    ],
)

TEACHER = workspace(
    "teacher",
    "Teacher",
    {"teacher"},
    [
        ("dashboard", "Dashboard", "essential"),
        ("timetable", "My Timetable"),
        ("classes", "My Classes"),
        ("attendance", "Attendance"),
        ("lesson-plans", "Lesson Plans"),
        ("weekly-progress", "Weekly Learning"),
        ("syllabus", "Syllabus"),
        ("assignments", "Assignments"),
        ("assessments", "Assessments"),
        ("class-teacher", "Class Teacher"),
        ("cbt", "CBT Practice"),
        ("learning-progress", "Learning Progress"),
        ("students", "Students"),
        ("excursions", "Excursions"),
        ("gallery", "Media Gallery"),
        ("messages", "Messages"),
        ("family-messages", "Family Messages"),
        ("ai", "Teacher AI"),
        ("performance", "My Performance"),
        ("profile", "Profile", "sensitive"),
        ("community", "Community"),
        ("my-duties", "My Duties", "sensitive"),
    ],
)
