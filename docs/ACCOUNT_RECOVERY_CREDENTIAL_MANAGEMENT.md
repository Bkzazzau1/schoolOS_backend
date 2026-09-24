# SchoolOS Account Recovery & Credential Management

## Purpose

SchoolOS automatically provisions Student and Parent accounts from canonical registration. This layer provides safe recovery and school-office credential management without making admission IDs or phone numbers sufficient to reset a password anonymously.

## Login identities

- Student login ID: permanent admission ID.
- Parent login ID: normalized Nigerian phone number.
- Proprietor/staff email login remains unchanged.
- A school-provisioned Student/Parent account starts with the person's first name as a temporary password and must replace it on first sign-in.

## Forgotten password

`POST /api/v1/credentials/recovery/request/`

The public endpoint always returns the same accepted response. It does not disclose whether an account exists. For a valid Student admission ID or Parent phone identity, SchoolOS creates a pending recovery request for the linked school office(s).

Recovery requests are rate-limited both per opaque identifier and by a broad network ceiling suitable for shared school networks.

## School-office management

Only an active Proprietor or Administrator membership in the school may use the credential-management endpoints.

Available actions:

- View the Student/Parent credential handoff state.
- Reset a Student password to the registered first name temporarily.
- Reset a Parent password to the account first name temporarily.
- Change a Parent login phone when that Parent identity belongs only to the current school.
- Review or dismiss pending recovery requests.

A password reset sets `must_change_password = true`. The user must create a private password before normal SchoolOS APIs are available again.

## Session revocation

`accounts.User.credential_version` is embedded in SchoolOS JWTs as `cv`.

Credential-sensitive administrative changes increment the user credential version. Tokens issued under an older version are rejected by `SchoolOSJWTAuthentication`, including access tokens derived from an older refresh token.

The first-login change from temporary password to private password does not increment the credential version because the currently authenticated bootstrap session is the session completing that required transition.

## Parent phone changes

A Parent phone change updates, in one database transaction:

- Parent login identity (the old phone stops resolving as a login ID).
- Linked guardian records for children in the school.
- Canonical Student registration guardian phone.
- Source admission application guardian phone when present.
- Synced `student_registration` payload and `parentLoginId`.
- Parent credential version, revoking existing Parent sessions.
- Credential audit history.

If the Parent account has active Parent memberships in another school, an individual school may not rename that global phone login. Platform-level support is required so every tenant can be reconciled deliberately.

Password reset remains available for a cross-school Parent identity because otherwise the shared account could not recover. A successful reset resolves all pending recovery requests for that global account.

## Credential handoff privacy

SchoolOS never stores or displays a private permanent password.

The credential handoff API returns a temporary password only while `must_change_password` is still true. Once the user has created a private password, the school sees only the login ID and that a private password has been set.

Administrators should hand temporary credentials to the student or guardian privately, not through public class groups or notice boards.

## Audit

Credential recovery requests and credential audit events are server records. Django admin exposes them read-only. Audit events never contain a plaintext password.
