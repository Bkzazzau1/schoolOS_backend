#: Job roles the owner can assign. Matches the app's jobRolePresets.
JOB_ROLES = frozenset(
    {
        "custom", "sectionHead", "finance", "administrator", "teacher",
        "driver", "cleaner", "security", "cook", "maintenance", "gardener", "support",
    }
)

#: Duties the owner can ask a person to carry out. Matches the app's
#: assignableDuties. These describe work; they are not access grants.
DUTIES = frozenset(
    {
        "operations.transport", "operations.cleaning", "operations.security",
        "operations.kitchen", "operations.maintenance", "operations.grounds",
        "operations.assigned_tasks", "operations.school_life",
        "finance.fees", "finance.collections", "finance.concessions",
        "finance.approvals", "finance.accounts", "finance.reconciliation",
        "finance.expenses", "finance.payroll", "finance.store", "finance.reports",
        "finance.bad_debt_classification", "finance.bank_connections", "finance.billing_authority",
        "administration.admissions", "administration.students",
        "administration.staff", "administration.attendance",
        "administration.communications", "academics.teaching",
    }
)

RECIPIENT_TYPES = frozenset({"registered", "unregistered"})
