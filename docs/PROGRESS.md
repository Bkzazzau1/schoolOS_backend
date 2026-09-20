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
| **2** | **Invitations and account linking**: emailed link, accept, web page, unlink, onboarding | **done** | 415 in total | `apps/invitations`, `contracts/invitations.md` ("As built") |
| **3** | **Sync pull**: devices download the records they may see | **done** | 438 in total | `apps/sync/pull.py`, `docs/features/sync-pull.md` |
| **4** | **Structure and appearance**: sections, leadership appointments, colour scheme | **done** | 462 in total | `apps/structure`, `docs/features/structure.md` |
| **5** | **Payroll batches**: prepare, approve or reject, instruct payment | **done** | 495 in total | `apps/payroll`, `docs/features/payroll.md` |
| **6** | **Scholarship approvals**: finance asks, owner decides | **done** | 515 in total | `apps/concessions`, `docs/features/concessions.md` |
| **7** | **Dashboards** (owner and finance): what the server really knows, plus "needs your attention" | **done** (partly) | 532 in total | `apps/dashboards`, `docs/features/dashboards.md` |
| **8** | **School-life modules** (16): community, noticeboard, events, transport, and the rest | **done** | 576 in total | `apps/schoollife`, `docs/features/school-life.md` |

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

## Item 2: invitations and account linking (done)

- Approving a proposal (or reopening a request) emails the person a single-use link, valid 14 days, from the
  school's official address, pointing at the school's own web address. Only a hash of the link is stored.
- Accepting creates or signs in the account, gives the role the owner approved, ties the login to the staff record
  (one login, one staff record, enforced by the database), activates authority and jobs assigned earlier, and tells
  the owner. The web page does the same for people without the app, then shows the registration form.
- The registration form, the app's `staff/me/onboarding/` and the sync path share one set of rules.
- Owner can see, resend (with a corrected email), cancel, and unlink.
- **Found and fixed**: with production settings and no `EMAIL_URL` the settings crashed on an empty URL; they now
  fall back to the fail-loudly backend.
- **Left**: documents cannot be uploaded from the web page yet (notes only); app screens not built.

## Item 3: sync pull (done)

- Every record carries a per-school change number that only goes up, handed out under a lock so changes are
  numbered and committed in order. `GET sync/pull/?school=&since=` returns the next page after a cursor.
- Each record type says who may read it (`visible`). Bank details are hidden from the administrator; salaries are
  the owner's alone; a proposal goes to its proposer, the owner and assigned approvers.
- Existing records are numbered by a data migration (tried on old-shape data).
- **Found and fixed**: two invitations created in the same clock tick could tie, so "newest" could be the revoked one.

## Item 4: structure and appearance (done)

- Sections, leadership appointments and the school's colour scheme: owner writes, the school reads (appointments
  only staff-side). Section rules (one head, same-section managers, no loops, HOD needs a department) are kept on
  the server.
- **App change needed**: it seeds default sections on the device without queuing them, so they must be sent first.
  Written up in `features/structure.md`.

## Item 5: payroll batches (done)

- The app's workflow, enforced on the server: lines rebuilt from salary records, approver is never the preparer (owner
  included), each step needs its own authority, approved batches are final, every step is in an audit trail.
- Salaries and batches now reach finance officers and authorised people through pull.
- **Left**: attendance clearance, recording real payment, approval thresholds. See `features/payroll.md`.

## Item 6: scholarship approvals (done)

- Finance (or anyone given the duty) asks; only the owner decides; decisions are final; amounts are fixed once sent; who
  and when are set by the server; the right people are told.
- Added `apps/owner/jobs/access.py` (does this person hold a duty the owner gave them) for later features to reuse.
- **Left**: student records, per-student limits, letting the owner delegate the decision. See `features/concessions.md`.

## Item 7: dashboards (done for the data that exists)

- Owner and finance summaries worked out on request from the real records: staff, payroll, scholarships and discounts,
  sections, and an attention list. The dashboards never invent figures: `notAvailableYet` names what needs students,
  attendance, results or fee payments, which are not on the server.
- Enrollment, reports and campus comparison wait for those records. Each will be added to the same endpoints.

## Item 8: school-life modules (done)

- 15 modules are one small spec each, run by one shared handler; community is four record types (post, comment,
  reaction, report) so nobody edits anyone else's record. Everyone in the school can read by default.
- Defaults I chose are listed in `features/school-life.md` (students cannot post; teachers add their own events; section
  and class audiences are not enforced yet).
- **App change**: community comments and reactions move out of the post into their own records.

## How to resume

```powershell
cd C:\Users\Mr. Bash\schoolOS_backend
git log --oneline          # each item is its own commit
.\.venv\Scripts\python manage.py test
```

Start the next item from the table above. Each feature follows the recipe in
`docs/architecture.md`.
