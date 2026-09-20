# Backend architecture

The backend is built **one feature at a time**. Each feature is a folder, each
file in it does one thing, and no file is longer than **700 lines** (a test
enforces it: `apps/core/tests/test_code_size.py`). When a file grows, split it
by responsibility into a folder; do not raise the limit.

## Layout

```
config/                 settings, root urls, api_v1.py (one line per feature)
apps/
  core/                 shared: Rejected, validation helpers, permission helpers, /health/
  accounts/             User (email sign-in) and the token endpoints
  schools/              School (tenant), Membership (role at a school), /me/, create_school
  domains/              each school's web addresses (platform subdomain + own domain)
    hostnames.py          strict host rules; what a school may and may not claim
    services.py           add, verify (DNS), set primary, disable, resolve a host
    middleware.py         which school is this request for?
    views.py              /.well-known/assetlinks.json (platform subdomains only)
  sync/                 the door every change from the app goes through
    registry.py           EntityHandler: how features plug their rules in
    services.py           applies one mutation (membership, conflicts, retries)
    models.py             SyncRecord (server copy) and MutationLog (audit + retries)
  owner/                FEATURE 1 (done)
    handlers.py           list of the feature's sync handlers
    payroll/              salary profiles, payroll authorizers
    jobs/                 job assignments
    views.py, urls.py     owner-only read endpoint
    tests/
docs/
  architecture.md       this file
  contracts/            agreed designs for features not built yet
```

Shared code lives in `core`, `schools` and `sync` and must not import from a
feature. Features import shared code, never each other. If two features need the
same thing, move it into `core`.

## How a change from the app is handled

1. The app POSTs one queued mutation to `sync/push/`.
2. `services.apply_mutation` checks the caller owns that membership at that
   school, replays a decision already made for that mutation id, and looks up
   the feature **handler** for the entity type.
3. `handler.authorize` decides whether this role may make this change at all.
4. Conflicts are checked against the record's version.
5. `handler.clean` validates the payload and returns **exactly what is stored**.
   Fields the server owns are set here, never copied from the app.
6. The result is recorded in `MutationLog` and returned as accepted, conflict
   or rejected.

Entity types with no handler are refused in production
(`SYNC_ALLOW_UNLISTED_ENTITY_TYPES`, on only in `DEBUG`). Refusing is safe;
guessing is not.

## Adding a feature (the recipe)

Take "staff" as an example.

1. `apps/staff/` with `__init__.py`, `apps.py`, and a folder per area
   (`proposals/`, `profiles/`, ...).
2. One handler per entity type in its own file, subclassing `EntityHandler`:
   set `entity_type` and `roles`, write `clean()` using `apps.core.validation`.
3. `handlers.py` listing them, and `AppConfig.ready()` registering each with
   `sync.registry.register` (copy `apps/owner/apps.py`).
4. If it has endpoints, `views.py` and `urls.py`.
5. Add `"apps.staff"` to `INSTALLED_APPS` and one `path(...)` line to
   `config/api_v1.py`.
6. `tests/` with one file per area. Cover: every role that must be refused,
   every bad value, every server-owned field the app must not be able to set,
   and cross-school isolation.
7. Run `python manage.py test`, `check`, and `makemigrations --check`.

## Rules every handler follows

- **Deny by default.** `roles` lists who may write; everyone else is rejected.
- **Copy known fields only.** Unknown keys the app sends are dropped.
- **The server owns identity and time.** Who acted, when, and any link to an
  account are set from the request's membership and the server's clock.
- **Append-only where history matters.** Salary history and reviews are only
  ever added to; the past is never rewritten.
- **No deletes for records that carry authority or money.** Revoke instead.
- **Validate strictly.** Whole numbers are whole numbers (not `"5"`, not `true`),
  lists are lists, choices are from a fixed set.
- **Messages a person can read.** A rejection's message is shown in the app.

## Build order

| # | Feature | State |
| --- | --- | --- |
| 0 | `domains`: school web addresses, DNS verification, app-link file | done |
| 1 | `owner`: salaries, payroll authority, job assignments | done |
| 2 | `staff`: proposals, approval (owner or assigned approver), staff profiles, identity uniqueness (phone and NIN) | next |
| 3 | `payroll`: batches (prepare, approve, release; approver is not the preparer) | |
| 4 | `invitations`: see `contracts/invitations.md` | contract written |
| 5 | Sync **pull** so devices download records | |
| 6 | `principal`, `administrator`, `finance`, `parent`, ... | |

Features 2 to 4 depend on each other: approving a proposal (2) creates the staff
record that an invitation (4) links to, and the delegated approvers from (2) and
(3) only take effect once (4) links their accounts.

## Known gaps

- The delegated approver flow in the app writes several records from the
  approver's device (directory entry, salary, profile). The owner-only rules in
  `apps/owner/` correctly refuse a non-owner writing a salary. Feature 2 must do
  approval **on the server, atomically**, instead of trusting the device to
  write those records.
- There is no pull endpoint except the owner's read of owner records.
- No file storage, no email backend, no background task runner yet.
