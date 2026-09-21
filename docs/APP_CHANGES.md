# What the Flutter app must change to use this backend

One checklist, gathered from every feature doc, in the order the work should be done. **None of it is started**: there is
no app integration yet, by design. Each feature doc has the detail; this page is the plan.

Base URL: `/api/v1/`. Everything except the invitation link and sign-in needs a signed-in user. Send
`?membership=<id>` on any request when the person holds more than one role at a school (the server answers
`400 membership_required` with the list if you forget).

## A. Foundation (everything else depends on these)

| # | Change | Detail |
| --- | --- | --- |
| A1 (**done**) | A real `SyncTransport` that sends queued mutations to `POST sync/push/` | Answers: 200 accepted, 409 conflict, 422 rejected (with a message to show), 403 not a member. Same mutation id always gets the same answer, so retries are safe. `docs/architecture.md` |
| A2 (**done**) | Sign-in, token refresh, and secure token storage | `auth/token/`, `auth/token/refresh/`, `me/` |
| A3 (**done**) | Download with `GET sync/pull/?school=&since=` | Start at 0, apply records, keep `cursor`, repeat while `hasMore`. `"deleted": true` means remove locally. Send the record's `version` back as `baseVersion` when editing. `features/sync-pull.md` |
| A4 (**done**, except local-data purge) | Access: call `access/me/` after sign-in and on every sync; hide screens not in the list; handle `blocking` (fetch, push, `access/acknowledge/`, then delete local data); treat a 403 as "no longer allowed" | `contracts/access-control.md` section 8 |
| A5 (**done**) | Notifications inbox with an unread badge | Opens on `access_changed`. In-app only for now |
| A6 | Show the server's message whenever a change is refused (422) | Every feature relies on this |
| A7 | Stop trusting anything the server owns | Statuses, "who did it", "when", account links and totals now come from the server. Show what comes back after the next pull |

**Contract note for the sender.** The server remembers its answer to every mutation id, so sending the same id again gets
the same answer even if the payload changed. A change edited after it was first sent, or edited after it was refused, must
go under a **new** id. (The app does this; see `schoolOS-app/docs/BACKEND_INTEGRATION.md`.) Changes must also reach the
server in the order they were first made, because some records point at others (an appointment needs its section).

A known gap: if someone loses access to a record, their device keeps its old copy until the record next changes.

## B. Staff (`features/staff.md`) (**done** in the app)

1. **Approve and reject proposals by calling the endpoints** `POST staff/schools/{school}/proposals/{id}/approve/` and
   `.../reject/`, not by writing records. They need a connection; offline, the owner sees the proposal and decides later.
2. **Stop pushing updates to `staff_proposal`.** They are refused. Read the result after the next pull.
3. The proposal form already has the system-role dropdown; keep it.
4. Registration: the staff member fills their details and bank account; only they can enter the account number.

## C. Invitations and account linking (`contracts/invitations.md`, "As built") (**done**, except opening the link from the phone: no Android project yet)

1. **Open the link.** Register the HTTPS app link (Android App Links, and the Windows equivalent) for the school's web
   address, and accept a pasted link. Without the app the link opens the web page instead.
2. Call `GET invitations/{token}/` (preview), then the **accept page**: set a password, or sign in if the email already
   has an account (`sign_in_required`), then `POST invitations/{token}/accept/`.
3. Store the returned tokens, select the school, pull the person's records. The onboarding banner then appears.
4. Onboarding from the app: `GET/POST staff/me/onboarding/`.
5. **Owner screen** for invitation status, resend (with a corrected email), cancel and unlink:
   `owner/schools/{school}/staff/{staffId}/invitation/` and `.../unlink/`.

## D. Structure and appearance (`features/structure.md`)

1. **Send the default sections and appointments.** The app seeds them on the device only, so the server never gets them.
   Until they are sent as `create` mutations (sections first, then appointments), editing a section or making an
   appointment is refused.
2. Replacing a head sends two updates; send the appointment first.

## E. Payroll batches (`features/payroll.md`)

1. Send `update` (the stored record with a new `status`) for each step; `create` only for a new month. If a device has no
   copy of a month and sends `create`, the server says it exists: pull, then try again.
2. Do not work out approver, preparer or totals as trusted values. Show what the server returns.
3. Salary records now reach finance officers through pull, so they can build a batch on their own device.

## F. Scholarships and discounts (`features/concessions.md`)

1. Choose a request number that will not collide across devices; on a conflict, choose another. The app now uses the
   number of requests on the device plus 41.
2. Decide with an `update`: the stored record, a new `status`, and a `decisionNote` (required to decline).
3. Stop seeding demo requests on the device (they were never sent, so deciding them is refused).

## G. Dashboards (`features/dashboards.md`)

1. The owner overview reads `GET dashboards/schools/{school}/owner/`; finance reads `.../finance/`.
2. Show anything listed in `notAvailableYet` (students, attendance, results, fee collection, campus comparison) as
   unavailable, or keep using device data for it. Never invent figures.
3. `attention[].screen` is an access-catalog key, so the app can open the right page. Refresh on open and pull-to-refresh.

## G2. School life (`features/school-life.md`)

1. **Community**: stop keeping comments and a reaction count inside the post. Create a `community_comment` per comment
   and a `community_reaction` (id `<postId>:<membershipId>`) per like, and count on the device. Reporting creates a
   `community_report`. Show the author from the record.
2. **Noticeboard**: never raise `readCount` on the notice; it is the server's.
3. Show the server's message when a guarded change is refused (`pinned`, review ticks, public showcase).
4. Widen the screens for roles that may now add records (teachers, staff, parents), and hide controls for the rest.
5. Meal days use the lower-case day as id; boarding records use an id made from the dorm name.

## H. Owner screens that are new

- **Access & Activities** (`owner.access`) (**done**): role defaults, a per-person page (grant, block after sync or now, end date,
  note), reassign, waiting blocks, audit list; warn before granting a `sensitive` activity.
- **Invitation status** (C5) and a **needs your attention** list (G3).

## Suggested order

A1 to A3 first (nothing works without sending and downloading), then A4 to A7, then C (people can only sign in through an
invitation), then B, D, E, F, G, H. Do D1 and F3 before testing those features, or they will look broken.

## Not on the server yet (so the app keeps using device data)

Students, attendance, academic results, fee payments and family accounts, campuses, and section or class audiences for school life. Records of these types are refused in production until their handlers exist.
