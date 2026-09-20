# Progress

Where the backend is, item by item. Updated as each item is finished, and each is
committed on its own so any point can be returned to.

**Working order** (from the owner review): 1 staff, 2 invitations and account
linking, 3 sync pull, 4 structure and appearance, 5 payroll batches, 6 scholarship
approvals, 7 dashboards, 8 school-life modules.

| # | Item | State | Tests | Notes |
| --- | --- | --- | --- | --- |
| - | Foundations: accounts, schools, sync door, domains, notifications, access (activities) | done | see below | Earlier work, all committed |
| - | Owner records: salaries, payroll authority, job assignments | done | | `apps/owner` |
| **1** | **Staff**: proposals, server-side approval, profiles, phone and NIN uniqueness | **done** | 339 in total | `apps/staff`, `docs/features/staff.md` |
| 2 | Invitations and account linking | next | | Contract: `contracts/invitations.md`. Depends on 1 (the `staff_approved` signal is ready) |
| 3 | Sync pull | not started | | Devices download their own records |
| 4 | Structure and appearance | not started | | Small, owner-only |
| 5 | Payroll batches | not started | | Approver is never the preparer |
| 6 | Scholarship approvals | not started | | |
| 7 | Dashboards (overview, finance, enrollment, reports) | not started | | Only after the data exists |
| 8 | School-life modules | not started | | One small handler each |

## Item 1: staff (done)

**Built**

- Sync hook `after_write` (same transaction, can undo the write) and
  `sync/records.py` for server-written records.
- Shared phone/NIN/name normalizing (`core/identity.py`), same rules as the app.
- `IdentityClaim`: database-enforced unique phone and NIN per school. Pending
  proposals hold their numbers.
- Handlers for proposals, the staff directory and staff profiles, with a
  role-by-section rule matrix (`profiles/rules.py`).
- Server-side approve and reject in one transaction; assigned approvers limited to
  teachers and support staff at the proposed salary; never their own proposals.
- Notifications to the owner, the proposer and reviewers.
- The `staff_approved` signal for the invitations feature.

**Verified**

- 339 backend tests pass (about 100 for staff, including a full journey from
  proposal to reviewed registration, a simulated race between two devices, and a
  forced failure halfway through approving that leaves nothing changed).
- Live server run with real sign-in tokens: proposals accepted, the same person
  refused when the phone is written a different way or the NIN with dashes,
  unauthorized approvals refused, approval created the salary, and the right people
  were told.
- `check`, `makemigrations --check` and the production deploy check are clean.

**Found and fixed on the way**

- A saved profile lost its `submittedAt` on the next save.
- Approving checked identity while the numbers were still held by the proposal.
- A person with two roles at one school is now asked which one is acting.

**Left for later** (listed in `features/staff.md`): heads of section proposing,
downloading records to other devices, staff attendance in the profile.

**App changes this needs** (not started; no integration yet): approve and reject
through the endpoints, stop pushing updates to proposals, send `?membership=`.

## How to resume

```powershell
cd C:\Users\Mr. Bash\schoolOS_backend
git log --oneline          # each item is its own commit
.\.venv\Scripts\python manage.py test
```

Start the next item from the table above. Each feature follows the recipe in
`docs/architecture.md`.
