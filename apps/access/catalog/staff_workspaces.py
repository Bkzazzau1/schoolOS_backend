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
        ("academics", "Academics"),
        ("students", "Students"),
        ("attendance", "Attendance"),
        ("approvals", "Approvals"),
        ("results", "Results & Reports"),
        ("timetable", "Timetable"),
        ("communication", "Communication"),
        ("incidents", "Incidents"),
        ("ai", "Principal AI"),
        ("performance", "School Performance"),
        ("profile", "Profile"),
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
        ("staff", "Staff Records"),
        ("staff-profiles", "Staff Profiles", "sensitive"),
        ("staff-attendance", "Staff Attendance"),
        ("records", "Records & Documents", "sensitive"),
        ("lifecycle", "Transfers & Promotion"),
        ("attendance", "Attendance Desk"),
        ("operations", "Operations"),
        ("notices", "Notices"),
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
        ("collections", "Smart Collections"),
        ("reminders", "Fee Reminders"),
        ("store", "School Store"),
        ("mandates", "Payment Mandates"),
        ("debt-aging", "Outstanding & Aging"),
        ("receipts", "Receipts"),
        ("accounts", "Student Accounts", "sensitive"),
        ("reconciliation", "Reconciliation", "sensitive"),
        ("expenses", "Expenses & Income", "sensitive"),
        ("payroll", "Payroll Handoff", "sensitive"),
        ("reports", "Reports"),
        ("ai", "Finance AI"),
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
        ("cbt", "CBT Practice"),
        ("learning-progress", "Learning Progress"),
        ("students", "Students"),
        ("messages", "Messages"),
        ("ai", "Teacher AI"),
        ("performance", "My Performance"),
        ("profile", "Profile", "sensitive"),
    ],
)
