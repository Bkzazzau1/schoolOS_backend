from .base import PROPRIETOR, workspace

# The owner's workspace. Screen keys match proprietor_workspace_page.dart.
OWNER = workspace(
    "owner",
    "Owner",
    {PROPRIETOR},
    [
        ("overview", "Executive Overview", "essential"),
        ("finance", "Owner Finance", "sensitive"),
        ("finance-approvals", "Concession Approvals", "sensitive"),
        ("enrollment", "Enrollment & Admissions"),
        ("staff", "Staff & HR"),
        ("jobs", "Jobs & Delegation", "sensitive"),
        ("staff-profiles", "Staff Profiles", "sensitive"),
        ("payroll", "Payroll & Salaries", "sensitive"),
        ("reports", "Executive Reports", "sensitive"),
        ("campuses", "Campus Comparison"),
        ("ai", "Proprietor AI"),
        ("structure", "Structure & Leadership"),
        ("appearance", "School Appearance"),
        ("school-life", "School Life"),
        # Decides who sees what, so it can never be handed to anyone else.
        ("access", "Access & Activities", "owner-only", "sensitive"),
    ],
)
