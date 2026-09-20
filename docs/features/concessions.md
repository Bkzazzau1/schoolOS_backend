# Feature: scholarships and discounts (backend)

Someone in finance asks for a scholarship or discount on a student's term fee; the owner approves or declines. Code:
`apps/concessions/`. The app sends `concession_request` records through `sync/push/`; other devices receive them through
`sync/pull/`. The same record type is used by the finance screen (asking) and the owner's approval queue (deciding).

## The steps

```
pendingApproval --owner approves--> approved
pendingApproval --owner declines--> declined   (a note is required)
```

A decision is final. A request cannot be deleted. Nothing reduces a family's fee until the owner approves.

## Who may do what

| Action | Who |
| --- | --- |
| Ask (create) | the owner; finance officers; anyone the owner gave the **"Scholarships and discounts"** duty (`finance.concessions`) through a job assignment, once their login is linked to it |
| Decide | **the owner only** |
| Receive requests (pull) | the same people who may ask; and a person always keeps the requests they raised, even if the duty is later withdrawn |

Nobody else, including the principal and the administrator, can ask, decide or receive requests.

## What the server does not take from the app

- **Amounts are fixed once sent.** Deciding cannot change the student, class, type, fee or amount: the owner approves
  exactly what was asked. The amount must be more than 0, at most the term fee, and the fee at most 100,000,000.
- **Who and when.** The requester (`requestedByMembershipId`, `requestedByRole`), the request date, the decider and the
  decision date are set by the server. `requestedAt` and `decidedAt` are in the short form the app shows ("02 Sep 2026");
  `createdAt` and `decidedAtIso` hold the exact time. A new request cannot arrive already decided.
- The request number can be any tidy id (for example `CNC-2026-041`). If two devices pick the same one, the second is
  told there is a conflict and must pick another.

## Who is told

The owner hears of each new request (unless the owner raised it). The person who raised it hears the answer, with the
owner's note (unless they decided it themselves).

## What the app must change (no integration yet)

1. Pick a request number that will not collide across devices, and retry with a new one on a conflict. The app now uses
   the number of requests on the device plus 41.
2. Decide by sending an `update` with the stored record, a new `status` and (for a decline) a `decisionNote`.
3. Stop seeding demo requests on the device: the seeded ones were never sent, so deciding them would be refused.
4. Show the server's message when a change is refused.

## Not covered yet

- Students are typed as names, not linked to student records (there are none on the server yet), so the same student can
  be given the same concession twice. A check will come with student records.
- A limit on total concessions per student or per fund, and a second approver above an amount.
- Letting the owner assign someone else to decide (as with approving staff and payroll). Today only the owner decides.
- Families seeing their own child's approved concession (needs the parent-student link).
- The administrator's requests: the app's demo data shows some, but the app itself only lets finance and the owner ask.
  The owner can give an administrator the duty above.
