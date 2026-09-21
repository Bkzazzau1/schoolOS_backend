"""Values the staff feature shares with the app. Each list mirrors the app's."""

PROPOSAL = "staff_proposal"
DIRECTORY = "administrator_staff_directory"
PROFILE = "owner_staff_profile"
SALARY = "owner_payroll_profile"
AUTHORIZER = "owner_payroll_authorizer"

#: The access role a new staff member can be appointed to. Never the owner, a
#: parent or a student. Mirrors staffSystemRoles in the app.
SYSTEM_ROLES = {
    "teacher": "Teacher",
    "driver": "Driver",
    "staff": "Support / other staff",
    "accountant": "Finance officer",
    "administrator": "Administrator",
    "principal": "Principal",
}

#: Roles someone the owner assigned to approve staff may approve. The others
#: reach money and student records, so only the owner approves them.
DELEGATE_ROLES = frozenset({"teacher", "driver", "staff"})

#: Who may propose new staff. (A head of section may too, once logins are linked
#: to job assignments; that link does not exist yet.)
PROPOSER_ROLES = frozenset({"proprietor", "principal", "administrator", "accountant"})

#: Who edits a staff record, and who may also send an onboarding request.
EDITOR_ROLES = frozenset({"proprietor", "principal"})
INVITER_ROLES = EDITOR_ROLES | {"administrator"}

STUDY_LEVELS = [
    "Secondary school certificate", "OND / NCE", "HND", "Bachelor degree",
    "Postgraduate diploma", "Master degree", "Doctorate (PhD)",
]

DEFAULT_DOCUMENTS = [
    "Passport photograph",
    "Government ID (NIN slip or passport)",
    "Highest academic certificate",
    "Professional licence or certificate",
    "Curriculum vitae",
    "Guarantor form",
]

DOCUMENT_STATUSES = ["requested", "received", "verified"]
FILE_STATUSES = ["Complete", "Missing document"]

# Onboarding request states.
NONE, INVITE_PENDING, SUBMITTED, REVIEWED = "none", "invitePending", "submitted", "reviewed"
ONBOARDING_STATES = [NONE, INVITE_PENDING, SUBMITTED, REVIEWED]

PENDING, APPROVED, REJECTED = "pending", "approved", "rejected"
