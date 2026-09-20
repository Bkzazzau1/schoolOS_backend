# Contract: staff invitation, link and account linking

**Status:** **built** (`apps/invitations`, `apps/staff/registration.py`), with 76 tests. Where the built version
adds to or differs from this text, see "As built" at the end. Approving a proposal sends the `staff_approved`
signal (`apps/staff/signals.py`) that this feature listens to. Written so the backend and the Flutter
app can be built to the same contract. Items marked **DECISION** are open and
need an answer before that part is built (collected in section 12).

**Decided by the school (recorded here):**

- **Role.** The role is the one proposed for the appointment. It is chosen when
  the appointment is proposed and approved by the owner. The new staff member
  never picks their own role (section 4, invariant 10).
- **Sender.** The invitation is sent from the school's **official email address**,
  which we create for the school.
- **The link** opens the app to an accept page where the person sets their
  password for future sign-ins. If the app is not installed, the same link opens a
  **web page** where they record their details instead (section 7.5).
- **Link domain.** Every school gets a **platform subdomain** automatically
  (`brightgate.schoolos.ng`) and may add **its own domain** later. All of it is
  managed in this backend (`apps/domains`, **built**). The emailed link uses the
  school's **primary** domain.
- **The web page** is a simple, server-rendered page inside this Django project.
  One codebase, no separate front end.

**Audience:** the backend developer building `apps/invitations/` (and the
`apps/staff/` handlers it depends on), and the Flutter developer building the
accept screen.

## 1. What this feature does

A school owner approves a new staff member. That person then:

1. receives an **email** from the school's official address, with a **link**;
2. opens the link: **in the app** if it is installed, otherwise **in a web
   page**. There they **set a password** (or, with an existing account, sign in);
3. is **linked**: their login is tied to their staff record at that school, and
   they are given the **role that was proposed and approved** for them;
4. **records their details** (personal details, bank account, documents),
   either in the app or on the web page, and submits them for review.

Steps 2 to 4 work the same whichever way they arrive. They set the password once
and use it to sign in next time, in the app or on the web.

Until step 3 nobody can act as that staff member, and the app already refuses
to let anyone else (including the owner) submit the registration form for them.

## 2. Terms

| Term | Meaning |
| --- | --- |
| **Staff record** | The synced record `owner_staff_profile`, entity id `STAFF-…`. Holds personal details, documents, bank details and `linkedMembershipId`. |
| **Membership** | A person's role at one school (`schools.Membership`). Its id is what the app sends as `membershipId`. |
| **Invitation** | A server record that says "this email may claim this staff record at this school, once, before this time". |
| **Token** | The secret in the link. Proves the holder received the email. |
| **Linking** | Setting the staff record's `linkedMembershipId` to the person's membership id, server side. |

## 3. What the app already does (facts to build against)

- On approval the app writes the staff record with
  `onboardingStatus: "invitePending"`, `onboardingEmail: "<email>"` and a
  checklist of required documents (all `requested`). It **never** sends email
  and never claims delivery.
- The registration form (`StaffOnboardingRepository.submit`) works only when
  `linkedMembershipId == the signed-in membership's id` **and**
  `onboardingStatus == invitePending`. Bank details can be changed only by the
  linked person. So the server must be the one to set `linkedMembershipId`.
- For authority the owner granted (`owner_payroll_authorizer`) and jobs
  (`owner_job_assignment`), the app treats a record as effective only when
  `status == "active"` and `membershipId == <the signed-in membership>`. Those
  two fields are server-owned. The sync handlers in `apps/owner/` already
  refuse any value the app sends for them.
- `onboardingStatus` moves `invitePending -> submitted -> reviewed`. Accepting
  an invitation does **not** change it: it stays `invitePending` until the
  person submits the form.

## 4. Invariants (must always hold)

1. **Only the server creates and sees tokens.** The app never generates,
   stores or sends one, except to pass along the one from the link.
2. **The account's email comes from the invitation, never from the request.**
   Whoever holds the link cannot choose a different email.
3. **A token is single use and expires** (default 14 days).
4. **Only a hash of the token is stored** (SHA-256). A database leak must not
   yield usable links.
5. **One staff record has at most one membership, and one membership has at most
   one staff record** (per school). Enforced by unique constraints, not code
   alone.
6. **An existing account must sign in to accept.** Holding the link is not
   enough to attach a staff record to someone's account.
7. **`linkedMembershipId`, authorizer `membershipId` and `status: active`, and
   job `membershipId` and `status: active` are server-owned.** Only linking sets
   them.
8. **Acceptance is atomic.** Either the account, membership, link and
   invitation state all change, or none do.
9. **Every state change is audited** (who, what, when, from where).
10. **The role is never chosen by the invitee.** It is fixed by the approved
    proposal. Letting people pick their own role would let anyone invited as a
    driver become an administrator.
11. **Both ways in run the same rules.** The app and the web page submit
    registration through the same server function, with the same validation,
    uniqueness checks (phone, NIN) and the same "only the linked person, only
    while the request is open" rule.

## 5. Data model

New app `apps/invitations/`.

```
StaffInvitation
  id              uuid pk
  school          FK School
  staff_id        text          # the STAFF-… entity id, not a FK: records are generic JSON
  email           text          # lower-cased, from the staff record
  role            text          # the approved system role (section 6a); copied from the proposal at approval
  token_hash      char(64) unique   # sha256 hex of the token; the token is never stored
  status          pending | accepted | revoked | expired
  expires_at      datetime
  created_by      FK Membership     # who asked (owner, principal or administrator), or null when triggered by an approval
  created_at      datetime
  sent_at         datetime null     # when the mail backend accepted it
  accepted_at     datetime null
  accepted_membership  FK Membership null

  constraint: at most ONE pending invitation per (school, staff_id)

StaffLink                          # the account-linking record (invariant 5)
  school FK, staff_id text, membership OneToOne
  linked_at, linked_via FK StaffInvitation
  unique (school, staff_id)        # one membership per staff record
  # membership is OneToOne: one staff record per membership

InvitationEvent                    # audit trail (invariant 9)
  invitation FK, at, event (created|sent|previewed|accepted|revoked|expired|resent|failed),
  ip, user_agent, detail json      # never the token
```

`School` (in `apps/schools`) gains `official_email` and `official_sender_name`
(the address invitations are sent from). Its web addresses live in
`domains.SchoolDomain` (**built**): several per school, one primary, each either
`platform` or `custom`. Emailed links use the primary domain (section 9).

`expired` is a state that is computed: a `pending` invitation past
`expires_at` behaves as expired everywhere, and a nightly job records it.

## 6. State machine

```
            create / resend
  (none) ─────────────────────► pending ──accept──► accepted   (terminal)
                                  │  ▲
                       expires_at │  │ resend: old one -> revoked, new one -> pending
                                  ▼  │
                               expired / revoked               (terminal)
```

- `resend` revokes the current pending invitation and creates a fresh one with
  a new token. Old links stop working immediately.
- An `accepted` invitation can never be reused. To move a staff member to a new
  login, the owner **unlinks** (section 8) and re-invites.

## 6a. The role a new staff member gets

The proposal already carries a free-text **job title** ("Mathematics Teacher").
It must also carry a **system role**, chosen from a fixed list by whoever
proposes, because the system role decides what the person can see and do.

| System role | Notes |
| --- | --- |
| `teacher` | Teaching staff. |
| `staff` | Support and other staff (drivers, cleaners, security, kitchen...). |
| `accountant` | Finance officer. |
| `administrator` | School administrator. |
| `principal` | Principal. |
| `proprietor` | **Never** assignable this way. |
| `parent`, `student` | **Never** assignable this way. |

Rules:

- The proposer picks the system role and the job title; the owner sees both when
  approving and **may change the role** while approving.
- **Settled (1a), implemented in the app:** an assigned approver (`approveStaff`) may approve
  only `teacher` and `staff`. `accountant`, `administrator` and `principal` need
  the owner, because those roles reach money and student records.
- The approved role is copied onto the invitation, so it cannot be changed by
  editing the proposal afterwards. Changing a role later is an owner action on
  the membership, and is audited.
- Extra responsibilities (section head, approver, payment authority) are **not**
  roles. They are the owner's job assignments and authorizations, which become
  effective at linking (7.3, step 6).

## 7. Endpoints

All under `/api/v1/`. JSON. Errors use a stable machine `code` plus a readable
`message` (section 10).

### 7.1 Creating an invitation

Not called by the app. The server creates one when a staff record becomes
`onboardingStatus: invitePending` with an email, that is, after the owner (or
an assigned approver) approves the proposal. Creation:

1. Generate `token = secrets.token_urlsafe(32)`; store `sha256(token)`.
2. Revoke any pending invitation for the same `(school, staff_id)`.
3. Queue the email (section 9). Do not send inside the request.
4. Store `sent_at` only when the mail backend accepts the message.

If the email is invalid or the send fails, keep the invitation, record a
`failed` event, and surface it to the owner (section 7.4).

### 7.2 Preview: what does this link open?

`GET invitations/{token}/`  ·  no sign-in needed  ·  rate limited

```json
200 {
  "schoolName": "BrightGate Academy",
  "staffName": "Musa Ibrahim",
  "email": "m****@school.ng",
  "expiresAt": "2026-10-04T12:00:00Z",
  "accountExists": true
}
404 { "code": "invitation_invalid", "message": "This link is not valid." }
```

- `404` with the **same body** for unknown, expired, revoked and already used
  tokens, so a caller cannot tell them apart or probe for valid tokens.
- `email` is masked. `accountExists` tells the app whether to show "create a
  password" or "sign in". It reveals whether that email has an account, but only
  to someone who holds a valid token for it, which is acceptable.
- Record a `previewed` event.

### 7.3 Accept

`POST invitations/{token}/accept/`  ·  rate limited

**New account** (no `Authorization` header):

```json
{ "password": "…", "firstName": "Musa", "lastName": "Ibrahim" }
```

**Existing account:** send `Authorization: Bearer <access>` for the account
whose email matches the invitation. The body may be empty. If the signed-in
account's email differs, respond `403 wrong_account`.

Server steps, in **one database transaction**:

1. Look up by `sha256(token)`; require `status == pending` and not expired,
   else `404 invitation_invalid` (or `409 already_accepted`, see below).
2. Resolve the user: create one with `invitation.email` and the given password
   (run Django's password validators), or use the signed-in user.
3. Create the `Membership(user, school, invitation.role)`, or reuse one that
   already exists with that role. The role comes only from the invitation
   (section 6a), never from the request.
4. Create the `StaffLink(school, staff_id, membership)`. If either side is
   already linked: roll back with `409 already_linked`.
5. Set `linkedMembershipId` on the staff record (`SyncRecord`), bump its
   `version`, record `updated_by = null` (system).
6. For every `owner_payroll_authorizer` and `owner_job_assignment` whose
   `staffId` / `registeredStaffId` is this staff id and whose status is
   `pendingActivation`: set `status: "active"` and `membershipId`, bump versions.
7. Mark the invitation `accepted` (`accepted_at`, `accepted_membership`).
8. Write an `accepted` event.

```json
200 {
  "access": "…", "refresh": "…",
  "membership": { "id": "…", "schoolId": "…", "schoolName": "…", "role": "staff" },
  "staffId": "STAFF-…"
}
```

- Replaying the request after success returns `409 already_accepted` with **no
  tokens**. A user who lost the response signs in normally.
- Wrong or weak password: `400 invalid_password` with the validator messages.

### 7.4 Owner endpoints

Owner, principal and administrator may send and resend (the same people the app
lets send onboarding requests). Only the owner may revoke or unlink.

| Method and path | Who | What |
| --- | --- | --- |
| `GET  owner/schools/{school}/staff/{staffId}/invitation/` | owner, principal, administrator | Current invitation: `{status, email, sentAt, expiresAt, lastEvent}`. Never the token. |
| `POST owner/schools/{school}/staff/{staffId}/invitation/` | owner, principal, administrator | Resend. Body `{"email": "…"}` optional, to correct the address. Returns the same status object. |
| `DELETE owner/schools/{school}/staff/{staffId}/invitation/` | owner | Revoke the pending invitation. |
| `POST owner/schools/{school}/staff/{staffId}/unlink/` | owner | Break the link (section 8). |

### 7.5 Recording details from the web (no app installed)

After accepting (7.3) the web page holds the returned tokens, so the person is
signed in. The page shows the same form as the app and submits it here. The
staff member never handles a token again.

`GET  staff/me/onboarding/`  ·  Bearer, the linked membership only

```json
200 {
  "status": "invitePending",
  "email": "musa@school.ng",
  "personal": { "phone": "", "nin": "", "address": "", "...": "" },
  "documents": [ { "name": "Passport photograph", "status": "requested" } ]
}
404 { "code": "no_open_request", "message": "There is no open registration request for you." }
```

`POST staff/me/onboarding/`  ·  Bearer, the linked membership only

```json
{
  "personal": { "phone": "...", "nin": "...", "email": "...", "address": "...",
                "dateOfBirth": "1990-05-14", "gender": "...", "stateOfOrigin": "...",
                "nextOfKinName": "...", "nextOfKinPhone": "..." },
  "payment":  { "bankName": "...", "accountName": "...", "accountNumber": "0123456789" },
  "documents": { "Passport photograph": "how or where it was provided" }
}
```

- The rules are exactly the ones the app applies on the device: phone is a valid
  Nigerian mobile and NIN is 11 digits, **both unique** in the school; address,
  date of birth and next of kin are required; the account number is 10 digits.
- Only the linked person, only while `onboardingStatus == invitePending`. It
  changes only that person's own details, bank details and document notes, never
  salary, reviews or credentials.
- On success `onboardingStatus` becomes `submitted`, the record's `version` goes
  up, and the owner and principal see it on their next sync.
- Errors: `404 no_open_request`, `409 duplicate_identity` (says which of phone or
  NIN, and the existing person's name, as the app does), `400 validation_error`.
- **Documents:** the web form records a note per document (where it was handed
  in), the same as the app. Uploading files needs file storage, which does not
  exist yet (DECISION 5).
- **One implementation.** The app's sync path (`owner_staff_profile` handler,
  linked member only) and this endpoint both call the same function in
  `apps/staff/onboarding/`, so the two cannot drift apart.

## 8. Account linking rules

- **Link:** created only by acceptance (7.3). Nothing else may set it.
- **Unlink:** the owner can unlink (for example the person left, or was linked to
  the wrong account). In one transaction: delete the `StaffLink`, clear
  `linkedMembershipId`, set the person's `owner_payroll_authorizer` and
  `owner_job_assignment` records back to `revoked`, deactivate the staff
  membership, write an audit event. It does **not** delete the user account.
- **Re-link:** by a fresh invitation only.
- **Removing a staff member** revokes their pending invitation and unlinks.
- **A person who is staff at two schools** has one membership and one link per
  school. Linking is always per school.
- **A person who is both staff and a parent** simply holds two memberships in
  the same school. Roles are never merged.

## 9. Email and the link

- **Sender:** the school's **official email address** (`School.official_email`,
  created by us for each school), with the school's name as the display name. The
  sending domain must be authenticated (SPF, DKIM, DMARC) or the mail lands in
  spam. Replies go to the same address.
- **Link:** one **HTTPS** link on the school's own domain:
  `https://{school.domain}/invite/{token}`. One link serves both cases:
  - **App installed:** the phone opens the app straight to the accept page
    (Android App Links). On Windows the app registers the same link.
  - **App not installed:** the browser opens the web page, which sets the
    password and records the details (7.5).
- **School domains (built in `apps/domains`).** Managed in the admin under
  *Domains*:
  - **Platform subdomain:** created automatically for every new school as
    `<short name>.PLATFORM_DOMAIN`, verified and primary. Reserved names (`www`,
    `api`, `admin`, `mail`, ...) are refused.
  - **Custom domain:** added by an operator, starts `pending` with a
    verification token. The owner publishes a DNS TXT record
    `_schoolos-verify.<domain>` with that value; the admin action *Check DNS and
    verify* then marks it `verified`. A custom domain can never be inside the
    platform domain, so a school cannot claim another school's address.
  - **Primary:** exactly one per school (enforced by the database), and it must be
    verified. Emailed links use it. The primary cannot be disabled until another
    is chosen.
  - **Which school is this request for?** `SchoolHostMiddleware` resolves the
    `Host` header to a school. Only verified domains of active schools resolve;
    pending or disabled ones behave as if they did not exist. Forwarded-host
    headers are not trusted.
  - **A token is valid on any verified domain of its own school**, and on no
    other school's domain (to be enforced when invitation lookup is built).
- **How the link opens the app.** Android opens a link in the app only for hosts
  declared in the app, and the app is built once for every school.
  - **Platform subdomains open the app.** The app declares one wildcard entry for
    the platform domain, and every school's subdomain serves
    `/.well-known/assetlinks.json` (built), which vouches for the app. It is
    served only when `ANDROID_APP_PACKAGE` and `ANDROID_CERT_SHA256` are set.
  - **Custom domains open the web page**, not the app: they never serve the app
    link file. The web page shows an **"Open in the app"** button using
    `schoolos://invite/{token}`, which the app also handles.
  - So a school whose primary domain is custom loses automatic app opening.
    Default to the platform subdomain as primary, and switch only if the school
    wants its own address in emails.
- **Still to build for the web page:** TLS for every domain (a reverse proxy with
  on-demand certificates, or a wildcard for the platform domain), and enforcing
  the domain in `ALLOWED_HOSTS` or a host-validation step once pages are served.
  Today the API works on any host and only the app-link file is host-specific.

- **Sending:** through Django's mail backend, from a background task, with
  retries and backoff. **DECISION 3a:** the mail provider that sends as the
  school's official address.
- **Content:** school name, the staff member's name and the role they were
  appointed to, who invited them, the expiry date, the link, and a line saying to
  ignore the email if unexpected. No salary, no NIN, no other personal data.
- **Never log the token** or the full link. Log the invitation id.
- Settings: `INVITATION_TTL_DAYS=14`, `PLATFORM_DOMAIN`, `ANDROID_APP_PACKAGE`,
  `ANDROID_CERT_SHA256`, and per school `official_email`. The link host is the
  school's primary domain; there is no global link base.
- The web page sends `Referrer-Policy: no-referrer` and `Cache-Control: no-store`.

## 10. Errors

| HTTP | `code` | Meaning |
| --- | --- | --- |
| 404 | `invitation_invalid` | Unknown, expired, revoked or used token (indistinguishable on purpose). |
| 409 | `already_accepted` | This invitation was already accepted. |
| 409 | `already_linked` | The staff record or the account is already linked. |
| 403 | `wrong_account` | Signed in as a different email than the invitation. |
| 400 | `invalid_password` | Password fails validation. `details` lists why. |
| 404 | `no_open_request` | Web registration: nothing is waiting for this person. |
| 409 | `duplicate_identity` | Web registration: the phone or NIN already belongs to someone else. |
| 400 | `validation_error` | Malformed body. |
| 429 | `rate_limited` | Too many attempts. |
| 401/403 | | Owner endpoints: not signed in / not allowed. |

## 11. Security requirements

- Token: `secrets.token_urlsafe(32)` (256 bits). Look up by hash. Compare hashes
  with `hmac.compare_digest`.
- Rate limits: preview 20/min per IP; accept 5/min per IP and 5/hour per token
  hash; resend 5/hour per staff record.
- After 10 failed accepts on a token hash, revoke it and notify the owner.
- HTTPS only. The link page must send `Referrer-Policy: no-referrer` and
  `Cache-Control: no-store` so the token does not leak.
- Passwords: Django validators (min 10, not common, not numeric).
- Accepting must not reveal whether an account exists to someone without a
  valid token.
- The audit trail is append-only and read-only in the admin.
- Sync handlers keep refusing app-supplied `linkedMembershipId`, `membershipId`
  and `status: active` (already true for the owner records; add the same for
  `owner_staff_profile` when that handler is written).

## 12. Decisions

**Settled**

1. **Role on acceptance:** the role proposed for the appointment and approved by
   the owner. Not chosen by the new staff member (6a).
2. **Sender and opening the link:** sent from the school's official email; opens
   in the app, or a web page if the app is not installed (section 9).

3. **Link domain:** the school's own domain, managed by us (`School.domain`).
4. **The web page:** a simple server-rendered page in this Django project.

5. **Domain structure (2b):** every school gets a platform subdomain and may add its
   own domain later; both managed in this backend. Built (`apps/domains`).

**Still open**

- **3a. Mail provider** that sends as each school's official address, and who
  creates those addresses (per school, at setup).
- **4. Invitation lifetime** (14 days assumed) and whether the owner can extend it.
- **5. Document upload.** Both the app and the web page can only record notes for
  documents until file storage exists. Is upload needed at launch?
- **7. Unregistered job assignees** (an email but no staff record): same flow,
  linking only the assignment? Suggested yes.
- **8. Notify the owner** when an invitation is accepted, a registration is
  submitted, or an email fails to send (email, in-app, or both)?

**Settled earlier (1a):** an assigned approver may approve only `teacher` and
`staff`; the owner approves `accountant`, `administrator` and `principal`. This is
implemented in the app.

## 13. Backend test plan (all must pass)

- Token: only the hash is stored; a token works once; expired and revoked
  tokens give the same `404` as unknown ones.
- Preview: masked email, no other data, same error for every invalid state.
- Accept, new account: user, membership, link, staff record, authorizer and job
  activation and invitation state all change together; a forced failure at any
  step leaves nothing changed.
- Accept, existing account: requires sign-in; wrong account is `403`; the link
  alone cannot attach a staff record to someone else's account.
- Replay after success is `409` with no tokens.
- One staff record and one membership can each be linked once (unique
  constraints fire under concurrent accepts).
- Resend revokes the old link, which then returns `404`.
- Unlink revokes authority and clears the link; re-invite then works.
- The app cannot set `linkedMembershipId`, `membershipId` or `status: active`
  through sync (every entity type that carries them).
- Role: the membership gets exactly the approved role; a request that names a
  different role is ignored; `proprietor`, `parent` and `student` can never be
  assigned this way; an assigned approver cannot approve a role above their limit
  (once decided, 1a).
- Web registration: works only for the linked person and only while the request
  is open; rejects a duplicate phone or NIN with the same messages as the app;
  cannot change salary, reviews or credentials; the app and web paths produce the
  same stored record for the same input.
- Email: sent from the school's official address; contains no salary, NIN or
  token in logs; a failed send is recorded and visible.
- Rate limits and the audit events exist for every state change.
- No log line, error message or response ever contains the token.

## 14. Work this contract needs

**Flutter app**

1. **Proposal form: DONE in the app.** It now has a required **system role**
   dropdown (section 6a) next to the job title, the owner sees and can change it
   when approving, and an assigned approver is limited to `teacher` and `staff`
   (recommended rule 1a). The proposal and the staff record carry `systemRole`.
   Originally this read: add a **system role** dropdown next to the job title. Today the proposal has only a free-text role, so it cannot say what
   access the person should get. The approve dialog shows it and lets the owner
   change it.
2. **Open the link:** register the HTTPS app link (Android App Links, and the
   Windows equivalent), and also accept a pasted link. Call **preview**.
3. **Accept page in the app:** set a password (or sign in for an existing
   account), then **accept**.
4. Store the returned tokens securely, select the school, and load the person's
   records (needs the sync **pull** endpoint, see `docs/architecture.md`).
5. The existing onboarding banner then appears, because `linkedMembershipId` now
   matches.
6. An owner screen for invitation status, resend and revoke (section 7.4).
7. A real `SyncTransport` calling `POST sync/push/` (does not exist yet).

**Backend**

1. `apps/staff/`: proposals with `systemRole`, approval, and
   `staff/onboarding/` (the one submit function both paths call).
2. `apps/invitations/`: the model, endpoints and email in this document.
3. `School.official_email` and per-school sending.
4. The web pages for accept and registration: Django templates, no separate
   front end.
5. **DONE:** `apps/domains` (domain model, DNS verification, host-to-school
   middleware, `assetlinks.json`, admin). Still to do: enforce allowed hosts once
   web pages are served.
6. TLS for every school domain (reverse proxy with on-demand certificates, or a
   wildcard for the platform domain).

## As built

- **Where things are.** `apps/invitations/` (models, tokens, mail, service, accept, unlink, listeners, public and
  owner views, server-rendered web pages and templates) and `apps/staff/registration.py` (the one function that
  submits a registration).
- **Public endpoints** (no sign-in): `GET invitations/<token>/` (preview) and `POST invitations/<token>/accept/`.
  Accept returns `access`, `refresh`, `membership {id, schoolId, schoolName, role}` and `staffId`.
- **Owner endpoints**: `GET/POST/DELETE owner/schools/<school>/staff/<staffId>/invitation/` (see, resend with an
  optional corrected email, cancel; cancel is owner only) and `POST .../unlink/` (owner only).
- **Onboarding from the app** after signing in: `GET/POST staff/me/onboarding/` (`?membership=` if the person holds
  several roles). Same rules as the web form and as the sync path: all three go through the staff profile handler.
- **Extra error codes**: `sign_in_required` (401, the email already has an account, so the person must sign in),
  `not_linked` (404, unlink), `invalid_email` (400), `not_found` (404). Bad, expired, revoked and replaced links
  are all the same `invitation_invalid` 404, and a link opened on another school's web address is one too.
- **Rate limits**: preview 20/min and accept 5/min per address (DRF throttles), plus 10 wrong tries per link and
  address per 15 minutes on the web page.
- **Email**: sent after the change is saved, from the school's `official_email`. If sending fails, the failure is
  recorded on the invitation (never the link) and the owner can send again. In production, without `EMAIL_URL`, every
  send fails loudly instead of silently doing nothing. Settings: `EMAIL_URL`, `DEFAULT_FROM_EMAIL`,
  `INVITATION_TTL_DAYS`, `INVITATION_LINK_SCHEME`, `INVITATION_FALLBACK_HOST`.
- **Still open**: file upload of documents on the web page (it records a note for each required document instead),
  and the app screens (accept page, onboarding form). No Flutter integration yet.
