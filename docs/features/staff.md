# Feature: staff (backend)

**Status:** built (`apps/staff`), not yet connected to the app. This is the server
side of Staff Profiles: proposing and approving staff, the staff record, and phone
and NIN uniqueness. It replaces what the app used to trust a phone to do.

## What changed for the app

| Before (on the device) | Now (on the server) |
| --- | --- |
| The approver's phone wrote the staff entry, salary, profile and decision itself | **One server action**: `POST staff/schools/{school}/proposals/{id}/approve/`. Everything happens or nothing does |
| Phone and NIN uniqueness checked against local data | **Enforced by the database**, so two devices working offline cannot both register the same person |
| The app could set a proposal's status, who proposed it, when | **Server sets them.** A proposal can only be created through sync, never edited or deleted |
| The app wrote `linkedMembershipId`, the person's role, review authorship | **Server-owned.** Ignored if the app sends them |

## Records and their rules

| Record (`entityType`) | Who may change it | Server-owned fields |
| --- | --- | --- |
| `staff_proposal` | **Create:** owner, principal, administrator, finance officer. **Update/delete: nobody** (decided by approve/reject only) | `status`, `proposedByMembershipId`, `proposedByRole`, `proposedAt`, decision fields |
| `administrator_staff_directory` | **Create:** owner only. **Edit:** owner (any field); administrator (file status only) | `staffCategory`, `systemRole`, `approvedFromProposal`, `createdByMembershipId`, `createdAt` |
| `owner_staff_profile` | See the matrix below | `linkedMembershipId`, `systemRole`, `updatedAt`, `updatedByMembershipId`, `submittedAt`, review author and time |

### Who may change which part of a profile

| Part | Owner, principal | Administrator | The staff member (linked login) | Anyone else |
| --- | --- | --- | --- | --- |
| Personal details | all | none | own, except employment date and type | none |
| Academics, credentials | all | none | none | none |
| Documents | all | only adding the standard documents with a registration request | mark a **requested** document as provided, and say how | none |
| **Bank details** | **none** | **none** | **own only** | none |
| Performance reviews | **add** (never edit or remove) | none | none | none |
| Registration request | send, resend, mark reviewed | send, resend | submit (while a request is open) | none |

"The staff member" means the login **linked** to that record. The server links it
when the person accepts their invitation (see `contracts/invitations.md`); until
then nobody can act as them, and the owner cannot enter bank details for them.

Every value is checked: phone is a valid Nigerian mobile (stored as `08031234567`),
NIN is 11 digits, dates are real, the account number is 10 digits, study level is
from the fixed list, ratings are 1 to 5, and so on. Submitting a registration
requires phone, NIN, address, date of birth, next of kin with a valid phone, and
complete bank details.

## Phone number and NIN are unique to one person

A table (`IdentityClaim`) records who holds each number in a school, with a unique
constraint. A **pending proposal holds its numbers too**, so nobody can be proposed
twice; approving hands them to the new staff member; rejecting frees them.

- A refused change says who has it: *"This phone number is already used by Musa
  Ibrahim (a pending proposal)."*
- Numbers are compared after normalizing, so `0803 123 4567`, `+234 803 123 4567`
  and `08031234567` are the same.
- Two devices racing: whichever syncs second has its **whole write undone** and is
  told who won.
- Another school may use the same numbers.

## Endpoints (under `/api/v1/`)

| Method and path | Who | What |
| --- | --- | --- |
| `POST staff/schools/{school}/proposals/{id}/approve/` | owner, or someone the owner assigned to approve staff | Body (optional): `{"gross", "deductions", "systemRole"}`. Returns `{"staffId", "alreadyApproved"}` |
| `POST staff/schools/{school}/proposals/{id}/reject/` | same | Body: `{"note"}`. Frees the numbers |

Add `?membership=<id>` if the person holds more than one role at the school.
Refusals are `400 {"code": "rejected", "message": "..."}`; not allowed is 403.

### What approving does, in one transaction

1. Checks the approver, the role and the salary.
2. Passes the proposal's phone and NIN to the new staff member.
3. Creates the **directory entry**, the **salary on payroll**, and the **profile**
   (with the person's phone, NIN and email, the standard required documents, and a
   registration request waiting to be sent).
4. Marks the proposal approved, tells the proposer.
5. After it commits, sends the `staff_approved` signal, which the invitations
   feature will use to email the person their link.

Repeating it is harmless. If any step fails, nothing changed.

### Who may approve

- **The owner:** always. May change the salary and the role.
- **Someone the owner assigned** the "approve new staff" authority, **once their
  login is linked** to that assignment. They may approve only **teachers and
  support staff**, at the **proposed salary**, and never their own proposal. An
  assignment nobody has claimed gives no power.
- Nobody else.

## People are told

| When | Who is told |
| --- | --- |
| A proposal is made | the owner |
| A proposal is approved or declined | the proposer, with the reason if declined |
| A staff member submits their registration | the owner and principal |

## What the app must do

1. **Approve and reject by calling the endpoints**, not by writing records. They
   need a connection; offline, the owner sees the proposal and decides once online.
2. **Stop pushing updates to `staff_proposal`** (they are refused). Read the result
   from the record after the next pull.
3. Send `?membership=` when the person has several roles.
4. Show the server's message when a change is refused (duplicate number, not
   allowed, and so on).

## Not covered yet

- **Heads of section** cannot propose staff. Their login is not linked to their job
  assignment until invitations exist; the app-side rule is ready for it.
- **Invitations and account linking**, so a staff member can act on their own
  record. Designed in `contracts/invitations.md`. Until then the owner and principal
  can edit a profile, but nobody can enter bank details or submit a registration.
- **Devices downloading these records** (sync pull). Only the owner's records can be
  read back today.
- **Staff attendance** (read into the profile) and the duplicates report screen.
- Staff already on a device but not on the server have no numbers on record, so
  uniqueness applies to them only once their profile is saved.
