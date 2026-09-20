# Feature: dashboards (backend)

Read-only summaries the owner (and finance) see on their home screens. Code: `apps/dashboards/`. Nothing is stored:
every figure is worked out on request from the records the server really holds, so it is never out of date and can
never disagree with them.

## Endpoints (under `/api/v1/dashboards/`)

| Path | Who | What |
| --- | --- | --- |
| `schools/{school}/owner/` | the owner only | `attention`, `staff`, `payroll`, `concessions`, `structure`, `notAvailableYet` |
| `schools/{school}/finance/` | the owner and finance officers | `payroll`, `monthlyPayroll`, `concessions`, `notAvailableYet` |

Add `?membership=<id>` if the person holds more than one role at the school. Not signed in is 401; the wrong role or
another school is 403.

## What is in them

- **attention**: the "needs your attention" list, most urgent first (`high`, `medium`, `info`). Each item has a `key`,
  `severity`, `title`, `count` and the `screen` that deals with it. An empty list means nothing is waiting. It covers
  staff proposals to decide, payroll batches waiting for approval or payment, scholarship and discount requests to
  decide, invitations that expired or never arrived, staff registrations to review, staff files missing documents,
  assignments the person has no login for yet, and sections without a head.
- **staff**: headcount, by section and category, files complete or missing documents, how many have a login, where
  registration stands (not requested, waiting for the staff member, waiting for review, reviewed), how many are on
  payroll, and the monthly gross, deductions and net of everyone on payroll.
- **payroll**: every month's batch (newest first) with its status, total and number of staff; what is waiting for approval
  and for payment; the total already instructed.
- **concessions**: pending (count and amount), approved (count, amount, students supported, by scholarship or discount),
  declined.
- **structure**: each section with its campus, classes, head and number of staff (staff are matched to a section by the
  section they are filed under: its name, stage or id), and how many staff are in no section.

## What it deliberately does not show

`notAvailableYet` lists what the app's dashboards show but the server cannot know yet, because those records are not on
the server: **students, attendance, academic results, fee collection, campus comparison** (finance: fee collection,
outstanding balances, family accounts). The app must show these as unavailable, or keep using its own device data for
them, and must not invent figures. As each of those features is built, its figures are added to the same endpoints.

## What the app must change (no integration yet)

1. The owner overview should show `attention`, `staff`, `payroll`, `concessions` and `structure` from this endpoint
   instead of demo data, and show the rest as unavailable.
2. `screen` values are access-catalog keys (a test checks each one exists), so the app can open the right page.
3. Refresh when the app opens and on pull-to-refresh; the numbers are always current.

## Not covered yet

- Trends over time (this is the state now; there is no history yet).
- The principal's and administrator's dashboards, and the executive report pack (exports).
- Campus comparison (needs a campus record).
