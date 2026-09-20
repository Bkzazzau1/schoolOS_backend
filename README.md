# SchoolOS backend

Django + Django REST Framework backend for the SchoolOS Flutter app
(`schoolOS-app`). The app is offline first: it saves changes on the device and
queues them, and this backend is where those queued changes are checked, stored
and shared between devices.

Built **one feature at a time**, each in its own folder, with no file over 700
lines. Read [docs/architecture.md](docs/architecture.md) before adding a feature.

## Run it locally

```powershell
cd C:\Users\Mr. Bash\schoolOS_backend
python -m venv .venv                     # once
.\.venv\Scripts\pip install -r requirements.txt
Copy-Item .env.example .env              # once; DEBUG=True for local work
.\.venv\Scripts\python manage.py migrate
.\.venv\Scripts\python manage.py create_school "BrightGate Academy" --owner-email you@school.ng
.\.venv\Scripts\python manage.py changepassword you@school.ng   # if the account is new
.\.venv\Scripts\python manage.py runserver
```

- Admin: <http://127.0.0.1:8000/admin/> (create a superuser with
  `manage.py createsuperuser` to use it).
- Health check: <http://127.0.0.1:8000/api/v1/health/>
- Android emulator reaches this machine at `http://10.0.2.2:8000`.

Tests: `.\.venv\Scripts\python manage.py test`

## Layout

| Path | Purpose |
| --- | --- |
| `config/` | Settings, root URLs, `api_v1.py` (one line per feature) |
| `apps/core` | Shared helpers: validation, permissions, `Rejected`, health |
| `apps/accounts` | `User`, signing in by email |
| `apps/schools` | `School` (a tenant), `Membership` (a role at a school), `/me/`, `create_school` |
| `apps/domains` | Each school's web addresses: a platform subdomain plus an optional own domain |
| `apps/notifications` | A person's in-app messages; any feature can notify people |
| `apps/access` | Which activities (screens) each person may see; the owner's grants, blocks and reassigns |
| `apps/sync` | Receives the app's queued changes; the registry features plug their rules into |
| `apps/owner` | Salaries, payroll authority, job assignments |
| `apps/staff` | Staff proposals, server-side approval, staff profiles, unique phone and NIN |
| `docs/` | Architecture, and contracts for features not built yet |

## API (all under `/api/v1/`)

| Method and path | Auth | What it does |
| --- | --- | --- |
| `GET  health/` | none | Liveness |
| `POST auth/token/` | none | `{email, password}` returns `{access, refresh}` |
| `POST auth/token/refresh/` | none | `{refresh}` returns a new `{access, refresh}` |
| `GET  me/` | Bearer | The person and their memberships |
| `POST sync/push/` | Bearer | Applies one queued change |
| `GET  owner/schools/{school}/records/{type}/` | Bearer, owner only | The owner's records of one kind |
| `GET  schools/{school}/access/me/` | Bearer | Which activities the caller may see, and blocks waiting for their app |
| `POST schools/{school}/access/acknowledge/` | Bearer | The app has synced; pending blocks take effect |
| `GET  schools/{school}/notifications/` | Bearer | The caller's own messages |
| `POST staff/schools/{school}/proposals/{id}/approve/` | Bearer, owner or assigned approver | Makes the proposed person a staff member, all at once |
| `POST staff/schools/{school}/proposals/{id}/reject/` | Bearer, owner or assigned approver | Declines it and frees their numbers |
| `…    owner/schools/{school}/access/…` | Bearer, owner only | Manage access: catalog, roles, people, reassign, audit |

Access tokens last 15 minutes and refresh tokens 7 days. Send
`Authorization: Bearer <access>`.

### `POST sync/push/`

Body is one mutation, in the field names the app's `SyncMutation` already has:

```json
{
  "id": "mutation id", "tenantId": "school uuid", "membershipId": "membership uuid",
  "entityType": "owner_payroll_profile", "entityId": "STAFF-001",
  "operation": "create | update | delete",
  "payload": {"...": "..."}, "baseVersion": 3
}
```

| HTTP | `disposition` | Meaning |
| --- | --- | --- |
| 200 | `accepted` | Applied. `serverVersion` is the record's new version. |
| 409 | `conflict` | The record changed on the server first, already exists, or was deleted. |
| 422 | `rejected` | Not allowed, not valid, or not accepted yet. `message` says why and is safe to show. |
| 400 | | The request itself is malformed. |
| 401 / 403 | | Not signed in, or not a member of that school with that membership id. |

- The caller must be signed in **and** own `membershipId`, an active membership
  at `tenantId`. Nothing is ever read or written across schools.
- **Retries are safe.** Every decision is stored against the mutation id.
- `baseVersion` is the version the app last saw. A mismatch is a `conflict`.
  Omitting it on an update is allowed (chained offline edits have none).
- What is stored is what the record type's **handler** returns, not what the
  app sent. Unknown fields are dropped and server-owned fields are set by the
  server.

### Owner records (feature 1)

Three record types, owner only. Anyone else's write is `rejected`.

| `entityType` | Rules the server enforces |
| --- | --- |
| `owner_payroll_profile` | Whole-number salary, deductions not above gross, on-payroll needs a salary. **History is append-only** and stamped by the server; the past cannot be rewritten. No deletes. |
| `owner_payroll_authorizer` | Authorities from a fixed list. **The app cannot make a grant `active` or set `membershipId`.** Only linking an account does. Revoke, never delete. |
| `owner_job_assignment` | Valid role, duties, registered or unregistered person, section for a head of section. Same server-owned status and link. Revoke, never delete. |

## Who sees what (activities)

There are 106 activities, one per app screen, and each role has a default set. The
owner can change a role's defaults for their school, give a person an activity,
block one, or move one between people, with optional end dates and a full audit
trail. School life is for everyone by default. A block waits for the person's app
to fetch and submit, then takes effect, and the person is told. Other features must call `require_activity()` so a blocked person cannot
reach the data by API. Contract: [docs/contracts/access-control.md](docs/contracts/access-control.md).
The owner's screens and how they map to backend work: [docs/features/owner.md](docs/features/owner.md).

## School web addresses

Every school gets `<short name>.PLATFORM_DOMAIN` automatically (set
`PLATFORM_DOMAIN` in `.env`). A school can add its own domain later, in the admin
under **Domains**:

1. Add the domain (kind `custom`). It starts `pending` and shows a DNS record to
   create: a TXT record named `_schoolos-verify.<domain>` with the given value.
2. Once the school has published it, select the domain and run **Check DNS and
   verify**.
3. Optionally run **Use as the school's primary domain** so emailed links use it.

Platform subdomains open links **in the phone app** (set `ANDROID_APP_PACKAGE` and
`ANDROID_CERT_SHA256`); a custom domain opens the web page instead.
Details in [docs/contracts/invitations.md](docs/contracts/invitations.md).

## Staff (built)

Proposals, approval, staff profiles and the rule that a phone number and a NIN
belong to one person only. Approval happens on the server in one step. Who may
change which part of a profile, and the app changes this needs, are in
[docs/features/staff.md](docs/features/staff.md). Progress: [docs/PROGRESS.md](docs/PROGRESS.md).

## Invitations and account linking (built)

Emailed single-use link, accept (app or web page), unlink, and the staff registration form.
Set `EMAIL_URL` in production. See [docs/contracts/invitations.md](docs/contracts/invitations.md) ("As built").

## Structure and appearance (built)

Sections, leadership posts and the school colour scheme. See [docs/features/structure.md](docs/features/structure.md).

## Payroll batches (built)

Prepare, approve (never by the preparer), reject, instruct payment. See [docs/features/payroll.md](docs/features/payroll.md).

## Scholarships and discounts (built)

Finance asks, the owner decides. See [docs/features/concessions.md](docs/features/concessions.md).

## Dashboards (built, for the data that exists)

Owner and finance summaries and the "needs your attention" list. See [docs/features/dashboards.md](docs/features/dashboards.md).

## For the app developer

Everything the Flutter app must change, in order: [docs/APP_CHANGES.md](docs/APP_CHANGES.md).

## Not built yet

See the build order in [docs/architecture.md](docs/architecture.md). In short:

- **Server-side handlers for everything except the owner records.** Other record
  types are accepted in DEBUG and **refused in production**, on purpose.
- Pull sync is built ([docs/features/sync-pull.md](docs/features/sync-pull.md)).
- File storage, background tasks.
- A Flutter `SyncTransport` that calls `sync/push/`.
- Postgres is supported through `DATABASE_URL` but has not been exercised.

## Production

Set `DEBUG=False`, a real `SECRET_KEY`, `ALLOWED_HOSTS` and `DATABASE_URL`. The
app refuses to start without a secret key when `DEBUG` is off. Serve it behind
HTTPS: `SECURE_SSL_REDIRECT`, secure cookies and HSTS are on when `DEBUG` is
off. Run `python manage.py check --deploy` before deploying.
