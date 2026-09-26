# Feature: scholarships and discounts (backend)

Someone in finance asks for a scholarship or discount on a student's term fee; the owner, or whoever the owner has given billing authority, approves or declines. Code:
`apps/concessions/`. The app sends `concession_request` records through `sync/push/`; other devices receive them through
`sync/pull/`. The same record type is used by the finance screen (asking) and the owner's approval queue (deciding).

## The steps

```
pendingApproval --approves--> approved
pendingApproval --declines--> declined   (a note is required)
```

A decision is final. A request cannot be deleted. Nothing reduces a family's fee until it is approved.

## Who may do what

| Action | Who |
| --- | --- |
| Ask (create) | the owner; finance officers; anyone the owner gave the **"Scholarships and discounts"** duty (`finance.concessions`) through a job assignment, once their login is linked to it |
| Decide | **the owner, or anyone holding the `finance.billing_authority` duty** (see `docs/SCHOOL_FEE_RECEIVABLES.md`). Asking, even with every other finance duty, never carries the power to decide |
| Receive requests (pull) | the same people who may ask, and billing authorities; and a person always keeps the requests they raised, even if the duty is later withdrawn |

Nobody else, including the principal and the administrator, can ask, decide or receive requests.

## What the server does not take from the app

- **Amounts are fixed once sent.** Deciding cannot change the student, class, type, fee or amount: the owner approves
  exactly what was asked. (For a request that names a real charge the fee is the school's own figure for it, not the app's.) The amount must be more than 0, at most the term fee, and the fee at most 100,000,000.
- **Who and when.** The requester (`requestedByMembershipId`, `requestedByName`, `requestedByRole`), the request date, the
  decider (`decidedBy` is their real name, or their role if they have none on file - never a hard-coded title;
  `decidedByRole` and `decidedByMembershipId` too) and the decision date are set by the server. `requestedAt` and `decidedAt` are in the short form the app shows ("02 Sep 2026");
  `createdAt` and `decidedAtIso` hold the exact time. A new request cannot arrive already decided.
- The request number can be any tidy id (for example `CNC-2026-041`). If two devices pick the same one, the second is
  told there is a conflict and must pick another.

## Who is told

Every billing authority (the owner and any delegate) hears of each new request (unless they raised it). The person who raised it hears the answer, with the
owner's note (unless they decided it themselves).

## Applied to a real charge

A request may name the charge it is for (`receivableId`, from the receivables ledger). Then the student, class and fee
come from the school's own records - not from the app - and the amount may not exceed what is still payable on that
charge. **Approving it takes the amount off the charge in the same transaction** (spread across the charge's
instalments; see `docs/SCHOOL_FEE_RECEIVABLES.md`), attributed to the person who decided and the person who asked, and
applying the same request twice makes one adjustment. If the adjustment cannot be made (the charge was voided, or a
larger award has since been made), the approval is refused and the request stays pending: a request is never
approved without its effect. A request that names no charge is the older kind - a decision on record only. Amounts in
the request are whole naira, as the app has always sent them; the ledger works in kobo.

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
- Families seeing their own child's approved concession (needs the parent-student link).
- The administrator's requests: the app's demo data shows some, but the app itself only lets finance and the owner ask.
  The owner can give an administrator the duty above.
