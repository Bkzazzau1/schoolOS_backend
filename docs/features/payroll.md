# Feature: payroll batches (backend)

A month's payroll, from preparing it to instructing payment. Code: `apps/payroll/`; who may do what is worked out in
`apps/owner/payroll/access.py`. The app sends batches through `sync/push/` (`payroll_batch`, id = the month, e.g.
`2026-09`) and other devices receive them through `sync/pull/`.

## The steps

```
prepared --approve--> approved --instruct--> disbursementInstructed
prepared --reject---> rejected --prepare again--> prepared
```

Anything else is refused ("A batch that is approved cannot become prepared"). An approved batch never changes again.
A batch cannot be deleted. **No step marks anyone as paid**; that needs real bank evidence and is not built.

## Who may do each step

| Step | Needs | Extra rule |
| --- | --- | --- |
| Prepare | `prepare` | |
| Approve | `approve` | **Never the person who prepared it, the owner included** |
| Reject | `approve` | A reason is required (300 characters at most) |
| Instruct payment | `pay` | Only on an approved batch |

The owner holds every authority. A finance officer always holds `view` and `prepare`. Anyone else needs the owner to
assign the authority **and** their login to be linked to that assignment (see invitations), so an assignment nobody has
claimed gives no power. `approve` or `pay` also give `view`. The authority to approve new staff is not a payroll power.

## What the server does not take from the app

- **The lines.** Each line's name and amount come from the salary records the owner set. A line whose amount does not
  match its salary is refused ("does not match their salary. Refresh and prepare again"), so the batch always says what
  the salary records say. Someone who is not on payroll, listed twice, or has no net pay is refused. The total is added
  up by the server.
- **Who and when.** Preparer, approver, rejecter, instructor and their times are stamped by the server. Anything of the
  kind the app sends is ignored.
- **A stale approval.** If any salary changed after the batch was prepared, approving is refused until it is prepared
  again, so nobody approves figures that are no longer true.
- Every step is appended to the batch's `trail` (who, what, when, and the reason for a rejection).

## Who is told

Preparing tells the owner and everyone who may approve. Approving tells everyone who may pay, the owner and the preparer.
Rejecting and instructing tell the owner and the preparer (the reason is included). Nobody is told what they just did.

## Who receives it

Batches and the salary records reach the owner, finance officers, and anyone holding a payroll authority (`view`,
`prepare`, `approve` or `pay`). Nobody else.

## What the app must change (no integration yet)

1. Send `update` (with the stored record and a new `status`) for each step, and `create` only for a new month. If a
   device has no copy of a month and sends `create`, the server answers "already exists": pull, then try again.
2. Stop working out approver, preparer and totals as trusted values; show what the server returns.
3. Show the server's message when a step is refused.
4. Salary records now reach finance officers through pull, so they can build a batch on their own device.

## Not covered yet

- Attendance: the app marks staff "ready" or "attendance review" from attendance data the server does not have yet, so
  the server cannot yet refuse a batch that includes someone under attendance review.
- Bank details are not on the lines, and there is no export or payment file.
- Recording that a salary was actually paid.
- Approval thresholds (for example a second approver above an amount).
