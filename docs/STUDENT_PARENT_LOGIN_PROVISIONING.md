# Student and Parent automatic account provisioning

Canonical student activation is the only authority that creates these school-user accounts.

## Student

When a registration is accepted as canonical Active:

- SchoolOS creates or confirms the canonical `Student` and active `StudentEnrollment`.
- A Student `User` and `Membership(role=student)` are created automatically.
- The student's login ID is the permanent **admission number**.
- The initial password is the student's **first name exactly as registered**.
- The Student membership receives a private server-generated `student_class_link` record from the canonical active enrollment.
- The registration sync payload is enriched with `canonicalActive`, `canonicalStudentId`, `credentialsProvisioned`, and `studentLoginId` only after the full transaction succeeds.

An admission ID that looks exactly like an email address or Nigerian phone number is rejected because SchoolOS uses one login field for email, admission ID, and parent phone.

## Parent / guardian

The primary guardian is provisioned in the same registration transaction:

- The login ID is the guardian's normalized Nigerian phone number (`+234...`).
- A new Parent account's initial password is the guardian's first name.
- Common honorifics such as Alhaji, Hajiya, Mallam, Dr, Engr, Prof, Mr, and Mrs are ignored when determining the first name.
- A Parent `Membership(role=parent)` is created for the school.
- `GuardianLink.account_user` links the account to the child.
- A private server-generated `parent_family_link` sync record lists that parent's real children for that school.

If another child is registered with the same verified parent phone/name, SchoolOS reuses the existing Parent user and password and adds the sibling relationship. It never resets an existing account to the first-name password.

A phone already bound to a different parent name is rejected for manual correction rather than silently merging families.

## Existing SchoolOS accounts

If the guardian already has a SchoolOS user (for example staff, proprietor, or a Parent from another child), the existing user/password is preserved. SchoolOS adds the Parent membership and phone login identity after identity consistency checks.

## First-login security

The first-name password is a bootstrap credential only.

New auto-provisioned Student/Parent users have `must_change_password=True`. After authenticating, JWT access is restricted to `/api/v1/me/` and `/api/v1/auth/password/initial-change/` until a new password passes Django's password validators. Normal school APIs remain blocked until that change succeeds.

No plaintext password is stored in sync records or returned by the API. The school knows the initial password deterministically from the registered first name.

## Authentication

`POST /api/v1/auth/token/` accepts one `identifier` field:

- proprietor/staff email;
- Student admission number; or
- Parent phone number.

The same Flutter login screen supports all three.

## Existing canonical students

Run:

```text
python manage.py provision_student_parent_accounts
```

This idempotently provisions accounts for existing canonical Active registrations, publishes their Student class and Parent family links, and enriches accepted registration sync records. Existing user passwords are never reset.
