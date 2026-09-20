# SchoolOS backend

Django + Django REST Framework backend for the SchoolOS Flutter app
(`schoolOS-app`). The app is offline first: it saves changes on the device and
queues them, and this backend is where those queued changes are checked, stored
and shared between devices.

## Run it locally

```powershell
cd C:\Users\Mr. Bash\schoolOS_backend
python -m venv .venv                     # once
.\.venv\Scripts\pip install -r requirements.txt
Copy-Item .env.example .env              # once; DEBUG=True for local work
.\.venv\Scripts\python manage.py migrate
.\.venv\Scripts\python manage.py createsuperuser   # asks for email + password
.\.venv\Scripts\python manage.py runserver
```

- Admin: <http://127.0.0.1:8000/admin/> (create a School, then a Membership for
  your user to give them a role at that school).
- Health check: <http://127.0.0.1:8000/api/v1/health/>
- Android emulator reaches this machine at `http://10.0.2.2:8000`.

Tests: `.\.venv\Scripts\python manage.py test`

## Layout

| Path | Purpose |
| --- | --- |
| `config/` | Settings, URLs, WSGI/ASGI |
| `apps/accounts` | Custom `User`, signing in by email |
| `apps/schools` | `School` (a tenant), `Membership` (a person's role at a school), `/me/` |
| `apps/sync` | Receives the app's queued mutations: `SyncRecord`, `MutationLog`, role policy |

## API (all under `/api/v1/`)

| Method and path | Auth | What it does |
| --- | --- | --- |
| `GET  health/` | none | Liveness |
| `POST auth/token/` | none | `{email, password}` returns `{access, refresh}` |
| `POST auth/token/refresh/` | none | `{refresh}` returns a new `{access, refresh}` |
| `GET  me/` | Bearer | The person and their memberships |
| `POST sync/push/` | Bearer | Applies one queued mutation |

Access tokens last 15 minutes and refresh tokens 7 days. Send
`Authorization: Bearer <access>`.

### `GET me/`

```json
{
  "id": "…", "email": "owner@school.ng", "name": "Ibrahim Yahaya",
  "memberships": [
    {"id": "…", "schoolId": "…", "schoolName": "BrightGate Academy", "role": "proprietor"}
  ]
}
```

Each membership has the same shape as the app's `SchoolMembership`, and `role`
uses the app's `SchoolRole` names (`proprietor`, `administrator`, `principal`,
`teacher`, `accountant`, `parent`, `student`, `staff`).

### `POST sync/push/`

Body is one mutation, using the field names the app's `SyncMutation` already
has:

```json
{
  "id": "mutation id", "tenantId": "school uuid", "membershipId": "membership uuid",
  "entityType": "owner_payroll_profile", "entityId": "STAFF-001",
  "operation": "create | update | delete",
  "payload": {"…": "…"}, "baseVersion": 3
}
```

| HTTP | `disposition` | Meaning |
| --- | --- | --- |
| 200 | `accepted` | Applied. `serverVersion` is the record's new version. |
| 409 | `conflict` | The record changed on the server first (or already exists, or was deleted). `serverVersion` is the current version. |
| 422 | `rejected` | Not allowed for this role or entity type, or the record does not exist. `message` says why. |
| 400 | | The request itself is malformed. |
| 401 / 403 | | Not signed in, or not a member of that school with that membership id. |

Rules:

- The caller must be signed in **and** own `membershipId`, an active
  membership at `tenantId`. Nothing is ever read or written across schools.
- **Retries are safe.** Every decision is stored against the mutation id, so
  resending after a lost response returns the same answer and applies nothing
  twice.
- `baseVersion` is the version the app last saw. If it differs from the
  server's, the result is `conflict`. Omitting it on an update is allowed,
  because chained offline edits (create, then update, before the first sync)
  have none.
- Which roles may write which entity types is set in `apps/sync/policy.py`.

## Read this before relying on it for anything sensitive

**Sync is not fully authorized yet.** Only the owner-only record types in
`apps/sync/policy.py` have rules. Every other type the app syncs is accepted
in DEBUG only (`SYNC_ALLOW_UNLISTED_ENTITY_TYPES`) and **refused in
production**, on purpose. Those types need server-side handlers first, because
the app's rules are finer than "role X may write type Y":

- `staff_proposal` - many roles propose; only the owner or an assigned approver
  may decide, and not on their own proposals.
- `owner_staff_profile` - only the staff member's own login may change bank
  details; owner and principal edit the rest.
- `payroll_batch`, `owner_payroll_authorizer` - prepare, approve and release
  payment are separate authorities, and the approver cannot be the preparer.
- staff onboarding - only the login linked to the staff record may submit.

The app enforces these on the device, but a device can be modified, so the
server has to enforce them too.

## Not built yet

- **Pull / download of records** to other devices. Only push exists.
- **Server-side handlers** for the workflows above.
- **Invitations:** sending the onboarding email, issuing a secure expiring link,
  and linking the new login to the staff record on activation. The app already
  queues the request (`onboardingStatus: invitePending`).
- Sending email at all (no mail backend is configured).
- File storage for passport photographs and documents.
- A Flutter `SyncTransport` that calls `sync/push/` (the app has the interface
  but no HTTP client yet).
- Postgres is supported through `DATABASE_URL` but has not been exercised.

## Production

Set `DEBUG=False`, a real `SECRET_KEY`, `ALLOWED_HOSTS` and `DATABASE_URL`. The
app refuses to start without a secret key when `DEBUG` is off. Serve it behind
HTTPS: `SECURE_SSL_REDIRECT`, secure cookies and HSTS are on when `DEBUG` is
off. Run `python manage.py check --deploy` before deploying.
