# SkulGen Account Recovery & Credential Management

## Purpose

SkulGen automatically provisions Student and Parent accounts from canonical registration. This layer provides safe recovery and school-office credential management without making admission IDs or phone numbers sufficient to reset a password anonymously.

## Login identities

- Student login ID: permanent admission ID.
- Parent login ID: normalized Nigerian phone number.
- Proprietor/staff login ID: email address. Recovers itself directly (below); it never routes through a school office.
- A school-provisioned Student/Parent account starts with the person's first name as a temporary password and must replace it on first sign-in.

## Forgotten password: Student and Parent (school-office mediated)

`POST /api/v1/credentials/recovery/request/`

The public endpoint always returns the same accepted response. It does not disclose whether an account exists. For a valid Student admission ID or Parent phone identity, SkulGen creates a pending recovery request for the linked school office(s).

Recovery requests are rate-limited both per opaque identifier and by a broad network ceiling suitable for shared school networks.

## Forgotten password: Proprietor and staff (self-service by email)

A Student or Parent account has no verified channel of its own, so its recovery goes through a human at the school office (above). A Proprietor or staff account signs in with its own verified email address, which is a channel on its own — no office needs to be involved.

`POST /api/v1/credentials/password-reset/request/` with `{"email": "..."}`. Always returns the same accepted response, disclosing nothing about whether the account exists. If the email belongs to a real, active account that was never provisioned with a Student admission or Parent phone login identity, a 6-digit code is emailed to it (same shape as email verification: `apps.accounts.email_verification`). Resending is capped by a short cooldown, independent of the broader rate limit.

`POST /api/v1/credentials/password-reset/confirm/` with `{"email": "...", "code": "123456", "newPassword": "..."}`. A wrong code counts against a 5-attempt ceiling before the code is dead outright; an expired or already-used code is refused the same way. A successful reset sets the new password directly (no temporary-password/must-change-password step, since the person chose it deliberately), increments `credential_version` to revoke any other already-issued sessions, and clears the code so it cannot be replayed.

Both endpoints are rate-limited per email address and by a broad network ceiling.

## School-office management

Only an active Proprietor or Administrator membership in the school may use the credential-management endpoints.

Available actions:

- View the Student/Parent credential handoff state.
- Reset a Student password to the registered first name temporarily.
- Reset a Parent password to the account first name temporarily.
- Change a Parent login phone when that Parent identity belongs only to the current school.
- Review or dismiss pending recovery requests.

A password reset sets `must_change_password = true`. The user must create a private password before normal SkulGen APIs are available again.

## Session revocation

`accounts.User.credential_version` is embedded in SkulGen JWTs as `cv`.

Credential-sensitive administrative changes increment the user credential version. Tokens issued under an older version are rejected by `SkulGenJWTAuthentication`, including access tokens derived from an older refresh token.

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

SkulGen never stores or displays a private permanent password.

The credential handoff API returns a temporary password only while `must_change_password` is still true. Once the user has created a private password, the school sees only the login ID and that a private password has been set.

Administrators should hand temporary credentials to the student or guardian privately, not through public class groups or notice boards.

## Audit

Credential recovery requests and credential audit events are server records. Django admin exposes them read-only. Audit events never contain a plaintext password.
