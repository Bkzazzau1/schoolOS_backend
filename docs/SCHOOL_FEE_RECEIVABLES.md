# SchoolOS School-Fee Receivables

## Purpose

What a **school charges its families**: fee schedules, what each student owes, what each family has paid, what it is
owed back, and the account a family pays into. Code: `apps/receivables/`.

This is **not** `apps/billing`. `apps/billing` is what SchoolOS charges *schools* (plans, subscriptions, usage). Nothing
in `apps/receivables` imports or reuses a billing model, and the names never overlap: a school's charge to a family is a
*receivable*, a *fee schedule* and a *statement*; SchoolOS's charge to a school is an *invoice*.

It is also not `apps/bankconnect`, which reads what the school's own bank accounts receive. The two meet in one place
(`apps/receivables/payments.py`): bankconnect decides who a payment is for, receivables turns that into money against
charges.

## Who decides what families owe

The proprietor controls who has authority to determine fees, and may delegate it to anyone eligible through the existing
owner job-assignment system. There is one new duty, `finance.billing_authority`; nothing else was added to the
permission system.

| Level | Who | May |
| --- | --- | --- |
| **Billing authority** (`can_manage_billing`) | the proprietor; any active holder of `finance.billing_authority` | draft, edit, publish, refresh, retire and clone fee schedules; adjust (discount, scholarship, waiver, correction), reverse an adjustment, void a charge; decide concession requests |
| **Operator** (`can_operate_receivables`) | the proprietor; the Finance Office; holders of `finance.billing_authority`, `finance.reconciliation`, `finance.accounts` or `finance.collections` | read families, charges and statements; make and change families; issue statements; correct where a payment went; refund credit; register and manage collection accounts |
| **Parent** | a parent whose signed-in account is linked to a guardian of the family's students | read their own family's statement and where to pay (`me/families/`) |

- A **job title never carries authority.** An Accountant, Bursar or Principal has none until the owner assigns the duty.
- **Parents, students and alumni can never hold either level**, whatever a stray record says: nobody decides their own
  family's fees.
- The duty is only valid **at the school it was given at**; revoked or not-yet-active grants give nothing; a membership
  from another school never counts (each service checks `actor.school_id`, not just the API).
- The duty is **explicit-only** in the app (`explicitOnlyDuties`): no role preset and no "all finance duties" shortcut
  includes it, so it is only ever given by choosing it.
- Every service function checks authority itself, so the API is never the only guard.

## What is stored, and what is derived

Money is whole minor units (kobo) everywhere, in integers, never floats. Every figure that could drift is **derived from
the ledger on read** and never stored:

```
gross        the amount charged at publication (fixed, immutable)
adjustments  discounts + scholarships + waivers + corrections, less any that were reversed
net          gross - adjustments                   what the family must actually pay for the charge
paid         bank allocations (not superseded) + family credit applied to it
outstanding  max(net - paid, 0)
status       void | settled (paid >= net) | partially_paid | open
```

A charge is **never over-paid**: money beyond `net` is not allocated, it becomes visible **family credit**. So a family's
outstanding can never go negative because of an untracked payment.

For a family: `outstanding` is the sum over its live charges; `credit` is the credit ledger's balance;
`collectible = max(outstanding - credit, 0)`. `StudentReceivable.status` is only a *cache*, refreshed inside every ledger
change (`lifecycle.settle_family`), and `ledger.verify_family()` checks the cache and every other invariant.

## The domain

| Model | What it is | Key rules |
| --- | --- | --- |
| `Family` | the household a school bills | fixed school-unique code (`FAM-K7Q2M9XA`); code and school never change; active/inactive |
| `FamilyStudent` | a student's membership of a family | **one ACTIVE family per student (database rule)**; leaving keeps the row with `left_at`; family and student must be at the same school |
| `FamilyGuardian` | who pays for a family | refers to the existing `GuardianLink` (no copy of name/phone); one primary payer per family |
| `FeeSchedule` / `FeeItem` | a fee structure for a session and term; its lines | draft is editable; **published is frozen** (service and model); item scope is typed: everyone, a section, one class, one student; optional items must name students; instalment `plan` in basis points |
| `StudentReceivable` | one charge a student owes (or one instalment of it) | gross and due date **immutable**; cannot be deleted (void it); **one per item + student + instalment (database rule)** so publishing twice or racing creates nothing twice |
| `ReceivableAdjustment` | an amount taken off a charge | always positive and always a reduction; append-only; an undo is a **reversal row**, never an edit or delete; `source_ref` makes the same decision apply once |
| `FamilyCreditEntry` | one movement of family credit | append-only; kinds `overpayment`, `released`, `application_reversed` (in) and `applied`, `refunded`, `overpayment_reversed` (out); never negative |
| `FamilyCollectionAccount` | the receiving identity one family pays into | provider-agnostic; `active` / `dormant` / `provisioning` / `suspended` / `closed`; never deleted |
| `FamilyStatement` | a numbered statement issued to a family | figures are always read from the ledger; the stored snapshot is reference only |
| `FinanceAuditEvent` | who did what to financial records | append-only; details scrubbed of anything that looks like a secret |

`bankconnect.TransactionAllocation` gained `family` and `receivable`; `BankTransaction` gained `receiving_account_ref`
(the account the money was paid into, when the provider says) and `family` (set only when known for certain).

### Instalments

An instalment plan is **several receivables sharing a `charge_key`**, each with its own due date and its share of the
fee (split in whole kobo so the parts add up exactly). No separate installment entity was added: each receivable stays
self-contained for allocation and audit. A scholarship or discount on a charge (`adjust_charge`) is **spread across the
charge's instalments** in proportion to what each still requires, so it affects the obligation consistently.

## Families, and the bridge from the old references

`family_account_ref` and `sibling_link` on registrations were typed at admission from a dropdown of labels; "Create new
family account" is the default. They are **not identifiers.** So the migration adds the new tables and guesses nothing.
`manage.py bridge_families` (report-only without `--apply`):

- groups students **only where they share exactly the same explicit reference** and none names a different one;
- never joins by name, phone, case-folding or similarity; never moves a student already placed; never reopens a family a
  person closed; never merges two families (a person can, see *Merging two families*);
- reports conflicts, students with no usable reference, and sibling hints for a person to look at;
- with `--singletons`, gives each remaining student a family of their own (never merged), which a person can join later;
- is safe to run again: it finds the family it made by its `origin` and extends it.

The old fields stay for compatibility. New finance work uses the canonical Family.

### Merging two families

Two households sometimes turn out to be one (siblings entered under different surnames, a guardian who registered twice).
`merging.merge(source, into, reason)` folds the source into the survivor so the school has one ledger and one place to pay:

- **Moves**: the students; the payers (a guardian both had is listed once, and there is still exactly one primary); every
  charge with its adjustments, payments and allocations; the credit ledger (so credit the source held pays what the merged
  household owes, straight away); the payments recorded for it; and its statements (numbers kept).
- **Accounts**: a payment account moves unless the survivor already has a live account with the same bank (a family holds one
  per bank). That account stays on the old family, which now points at the survivor, and money paid into it is credited to the
  survivor - **no number a family was given ever stops working**. The account follows what the merged household owes.
- The old family is kept, `INACTIVE`, with its code and `merged_into`; it is never deleted. Merges stay one level deep
  (merging A into B and then B into C makes A point at C). It is **not reversible**: undoing it would mean re-deciding which
  moved payments belonged to whom.
- One transaction; both families locked in a fixed order; the result is checked against the ledger's invariants before it is kept.
- **Billing authority only** (the owner or a delegate), with a reason, audited. The finance office may preview
  (`GET families/<id>/merge-preview/?into=<id>`: who and what moves, which accounts move or stay, anything that would refuse it).
  `POST families/<id>/merge/ {intoFamilyId, reason}` does it.

## Fee schedules

Only a billing authority drafts, edits and publishes. **Publishing** fixes the schedule and raises one charge per
student per item (or instalment) for everyone it applies to.

- **Applicability comes from the academics app** (`EnrollmentAcademicContext`), never from class names typed by a client.
  A billable student the academics records cannot place for the session is **reported and not charged**; so is a
  student with no family. Which period a schedule is for, and who is charged for it, follows the calendar - see
  *Sessions and terms*. A later `refresh` charges them once fixed (and any student who joined after publication). It
  only ever creates what is missing.
- Corrections after publication are explicit: `retire` (with a reason; charges stay), `clone` (a new draft), `adjust`,
  `void`, `void-charges`. A schedule that **replaces** another cannot be published until the original is retired, and
  never charges a student again for an item the original already charged (that is an adjustment, not a second charge).
- `preview` shows who would be charged and how much without charging anyone.

## Sessions and terms

The fee system follows the school's **canonical academic calendar** (`apps.academics`: `AcademicSession`, `AcademicTerm`,
`EnrollmentAcademicContext`). It never asks a client for a period by name or date. All of it lives in one module,
`apps/receivables/periods.py`, and its rules are:

- **A schedule belongs to a session and, usually, one of its terms** (no term = the whole session). Posting a schedule with
  no session names the school's **current period** - its active session and that session's active term (or the whole
  session with `wholeSession: true`). With no active session the answer is a refusal, `no_active_session`, that says to
  choose one; the same for `no_active_term`.
- **A closed session or term is history and is never billed** (`period_closed`): it cannot be given a schedule, a draft
  cannot be published into it and a published schedule cannot be refreshed for it. What was already charged stays a live debt
  - it can still be paid, adjusted, voided and reallocated.
- **A student owes nothing for a term that began before they were enrolled.** They are placed by the term they entered in
  (`EnrollmentAcademicContext.entry_term`, which the academics app records at enrolment); where none was recorded, by the day
  their enrolment started. A fee for a term reaches only students enrolled by then. Those left out are **listed, not
  hidden**, as `joinedLater` in the preview and the publish report, and are not "unclassified", so the report still reads as
  complete. A fee for the whole session is owed by everyone in it. A fee aimed at one named student is theirs whenever they
  joined: that is how a late arrival is deliberately charged for a past term.
- **A due date belongs to its period** (`due_date_outside_period`): not after the period ends, and not more than 45 days
  before it begins (fees are asked for ahead of a term). Every instalment is held to the same window, on creating and on
  changing an item, and `preview` reports it if a term's dates were moved afterwards. Instalments that run past a term go on a
  schedule for the whole session.
- **Arrears** are what is still owed for a period that has **ended** - its last day has gone, or the school has closed it.
  What is owed for a period still running is `current`, whether or not it is overdue. Both are derived, on the family
  position and on every report, and add up to `outstanding`.
- **The clock** is read in one place (`periods.school_today()`, the school's calendar day in Africa/Lagos), so tests can fix it.

### Reports by term

Every figure is derived from the ledger, never stored (`apps/receivables/reports.py`), and grouped by the canonical session and
term of each charge, in calendar order:

- `GET calendar/` - the school's sessions and terms, which are current, and the due-date lead: what a schedule can be made for.
- `GET reports/terms/` (optional `?session=`) - for each session/term: charges, gross, adjustments, net, paid, outstanding,
  overdue, students, families, families still owing, and the **collection rate** (basis points of what was payable that has
  been paid), with totals and arrears.
- `GET reports/position/` - what the school is owed now: outstanding, overdue, arrears, current, credit held by families
  (kept apart, never netted off), families owing, and the same by period. `available` is false until the school has raised any
  charge, so a school with no ledger sees "not available", not zeros.
- Every charge and statement line carries `sessionName` and `termName`; a family statement adds a `byPeriod` roll-up and
  arrears/current in its position; `charges/` and `fee-schedules/` filter by `?session=` and `?term=`.
- The same position is the `receivables` block of the collections summary, and so of the owner and finance dashboards.
  `outstandingFeesAvailable` is true exactly when it is available, and "outstanding balances" leaves the finance dashboard's
  `notAvailableYet` list then.

## Adjustments and concessions

Discounts, scholarships and waivers never overwrite the gross. Each is a separate row with who requested it, who
authorised it, when and why. An adjustment can never exceed what is still payable. If it leaves a charge needing less than
has been put towards it, the excess is **released as family credit** (credit applications are undone first, newest
first, then bank allocations), never discarded.

**Concessions** (`apps/concessions`) are still the `concession_request` sync record, but:

- they are now decided by the owner **or any billing authority**, and the record names the person who actually decided
  (their name and role), not a hard-coded "Proprietor";
- a request may name a charge (`receivableId`): then the student, class and fee come from the school's records, and
  **approving it takes the amount off that charge in the same transaction** (spread over instalments). If that cannot
  be done the approval is refused and the request stays pending. Applying the same request twice makes one adjustment;
- a request that names no charge is the older, record-only kind.

## Family credit

Credit arises from an overpayment or a release. It is **used automatically** against what the family owes (credit and
debt never sit side by side) and never counted as earned revenue. `refund` records credit paid back (finance office,
with a reason; the money itself is paid out by the school). If a payment is reversed after its credit was used, the uses
of it are undone newest first, so the charges it paid are owed again; if the credit was already *refunded* the reversal
is refused and nothing changes.

## Payment allocation

`allocation.allocate(tx, family)`:

1. takes the family's row (`select_for_update`) so two things happening to one family are handled one after the other;
2. works out what the payment has left: amount less what is already allocated to charges and held as credit, so a
   **payment can never be allocated twice**;
3. pays charges in policy order, never more than a charge needs;
4. holds any excess as family credit;
5. settles the family: credit is used, statuses refreshed, the collection account made active or dormant, people told.

The default policy (`policy.py`) is: oldest overdue, then oldest due now (within 30 days), then next unpaid; ties broken by
due date, creation time and id, so the same payment always lands the same way; a student can be preferred. Replace it with
`RECEIVABLES_ALLOCATION_POLICY` (a dotted path to a function with the same signature). The finance office can move a payment
exactly where it belongs (`correct_allocations`): the old allocation stays on record, superseded, and the correction
writes a decision on the payment's own history.

Credit-funded settlement is recorded in the credit ledger (`applied`), not as a `TransactionAllocation` (which needs a bank
transaction): a charge's `paid` is the sum of both.

## Collection accounts

**The account belongs to the family, never to a child.** A family may have several children; they share one place to pay,
and the ledger shares what arrives across whichever of them owe (see *Payment allocation*). A child's id or student code is
never an account number.

**What an account looks like depends on the bank.** Providers do not all give a ten-digit number: one gives an account
number, another a payment code or wallet id, another a reference to quote alongside a shared account. Nothing assumes a
format. Each provider has an `AccountShape` (`account_shapes.py`): what it *calls* the identifier (`numberLabel`), what one may
look like (broad by default; narrowed only for a provider that has documented its format, never guessed), and a one-line
`payerNote`. An account also carries `details` - extra facts the payer must be told, as label/value pairs (a payment
reference, a sort code) - and a family may hold accounts with several banks at once (one live account per family per provider).

**Where an account comes from** (`issuers.py`):
- *Recorded by hand* - the finance side records the account a bank gave a family (`POST families/<id>/collection-accounts/`),
  for any provider and any format.
- *Issued by a provider adapter* - `POST families/<id>/collection-accounts/issue/` (or `collection-accounts/issue-missing/` for
  every active family without one) asks the school's own connected provider to make it. **No real bank adapter exists yet**: a
  real one is written only against that bank's published documentation, so every listed bank answers `issuer_unavailable` and
  says to record it by hand. The **sandbox** issuer (only where the sandbox is on) makes clearly-labelled test accounts so the
  whole path - issue, show the parent, receive a payment, settle the family - is exercised end to end.
- `GET collection-accounts/providers/` lists the providers with their shapes and whether each can issue, so a form can adapt.

**What a parent is shown** (`me/families/`, `me/families/<id>/statement/`): the family's accounts, once, with `numberLabel`,
`details`, `note`, `status`, `canPay` and `isTest`. An account still being set up, or paused by the school, is listed but its
number is withheld (`canPay: false`), so a family is never sent to pay one that may not receive. Provider internals are never shown.

**School fees are the school's money.** Every account here is the school's own provider account under one of its connections;
SchoolOS does not receive, hold or settle it. What schools pay SchoolOS (the SaaS subscription) is a separate matter
(`apps/billing`), and no school-fee path uses SchoolOS's own payment provider.

**Lifecycle.** Provider-agnostic. After **every** ledger change: family owes something -> the account is **ACTIVE**; owes nothing ->
**DORMANT**. A dormant account is not closed or deleted: it keeps its provider identity and the **same** account is active
again when new fees are published. Accounts that are still being set up, suspended by a person, or closed are never changed
by a settled bill. How dormant is enforced (the provider refusing transfers, or SchoolOS only flagging what arrives) is for
a provider adapter; this core only holds the status and the receiving identifier.

## How bank payments are matched

A payment made into a known family account is **that family's, with certainty**: `find_by_receiving_reference` matches the
account the provider reported (by account number or provider reference, scoped to the school and provider; closed
accounts identify no one). The engine matches it at 100% with no narration guessing and skips the duplicate heuristic.
Payments with no such reference use the existing fuzzy student matching unchanged.

A match, automatic or a person's decision, becomes money against the family's charges inside the same transaction. A
person's decision always wins over the account. A payment that arrives on a family owing nothing is held as credit and
flagged for review. Every review decision first takes the payment back out of the ledger. A school with no families
behaves exactly as it always did.

## API

Under `/api/v1/schools/<school>/receivables/` (all school-scoped; another school's object looks like it does not exist;
a refusal is a 400 with a stable `code` and words a person can act on):

- **Families** (operator): `families/` (GET search, POST create), `families/<id>/`, `.../rename|add-student|remove-student|link-guardian|set-status/`, `.../receivables/`, `.../statement/` (GET, derived), `.../statements/` (GET, POST issue), `.../credit/`, `.../credit/refund/`, `.../payments/`, `.../collection-accounts/` (GET, POST register), `.../collection-accounts/issue/` (POST), `.../merge-preview/` (GET), `.../merge/` (POST, billing authority), `collection-accounts/providers/` (GET), `collection-accounts/issue-missing/` (POST), `statements/<id>/void/` (POST), `families/unassigned-students/`, `families/bridge/` (GET report, POST apply); `collection-accounts/<id>/suspend|reinstate|close|mark-provisioned/`.
- **Fee schedules** (read: operator; change: billing authority): `fee-schedules/` (filter `?status=&session=&term=`; a POST with no `sessionId` is for the current period), `.../<id>/`, `.../preview/`, `.../rename|publish|refresh|retire|clone|void-charges/`, `.../items/`, `.../items/<item>/update|remove/`.
- **Calendar and reports** (operator): `calendar/`, `reports/terms/`, `reports/position/` (see Sessions and terms).
- **Charges and decisions**: `charges/` (filters incl. `?session=&term=`, and paging), `charges/<id>/`, `charges/<id>/adjust|void/` (billing authority), `adjustments/` (history), `adjustments/<id>/reverse/` (billing authority).
- **Payments**: `payments/<transaction>/reallocate/` (operator).
- **Parents**: `me/families/`, `me/families/<id>/statement/`.

Concession request, approve and decline stay on the sync entity (`concession_request`); `adjustments/` is their history.

## Audit and notifications

Every sensitive step writes an append-only `FinanceAuditEvent` (actor, school, object, before/after where safe): schedule
created, item added/changed/removed, published, refreshed, retired; adjustments applied and reversed; charges voided;
payments allocated, released and corrected; credit applied and refunded; collection account registered and every status
change; statements issued; families made, changed and bridged.

Notifications reuse the existing inbox and are targeted, not broadcast: the finance side (owner, finance office, and
authorised delegates) hears of fees published, an overpayment held as credit and a collection account waking up; a family's
parent accounts hear of new fees, a payment received and fees settled. Test data is labelled.

## Tenancy, idempotency and offline

- Every table carries the school; models refuse a family, student, receivable, allocation or credit entry that crosses
  schools, and the services check again. A payment from another school can never pay this school's charge.
- Idempotent by construction: one receivable per item + student + instalment; one adjustment per source; one credit entry per
  reference; one statement number per school; a payment allocated once.
- Publishing, bank ingestion, allocation, adjustments and account provisioning are **server-authoritative**: none is
  offline-first. Concession requests remain sync records (the app can queue them), but a queued request is not a decision
  and not a ledger change until the server accepts it.

## Verification

`manage.py test apps.receivables` (424 tests) covers: authority (every role, revoked/pending duties, other schools);
families and the bridge; fee schedules (every rejection, freezing, applicability, instalments, idempotent publishing);
the academic calendar (current period defaults, closed periods never billed, students who joined after a term, due-date
windows, reports and arrears by term, dashboards, statements by period; the clock is fixed in these tests);
family payment accounts (shapes per bank, several banks per family, what a parent is shown, provider issuers, statement voiding); merging
families (everything moves, the ledger adds up, old account numbers keep working, chains, authority);
adjustments, reversals, voids and credit release; credit; allocation policy, reversals and corrections; collection account
lifecycle; bank integration, including the real public webhook route into a family account (a repeated or forged
delivery pays nothing twice); concession integration; statements; notifications; every API endpoint against a second
school; and a **seeded random sequence of every operation with the whole ledger's invariants and conservation of money
checked after each step**. Run once at larger scale (60 seeds x 40 steps) with no violation.

## Assumptions and what is deliberately not here

- **Direct-debit mandates** (Remita, Lendsqr) are not implemented. The ledger answers what a mandate integration will ask:
  who owes (`family_position`), how much, what is due (`outstanding`, `overdue`), what has been paid, what credit exists,
  and whether anything is collectible (`collectible`).
- **No real provider adapter issues family accounts yet.** The framework, the per-bank account shapes and the sandbox issuer
  exist; a real bank's issuer is written only against its own published documentation. Until then the finance side records
  the account the bank gave a family by hand.
- Currency is NGN only.
- A student removed from a family leaves the charges already raised with the family that was billed; a family set inactive
  raises no new charges but its existing ones stand. Two families can be merged (see above); a merge is not reversible, and the
  bridge never merges anything on its own.
- A statement issued in error is voided (`POST statements/<id>/void/`, with a reason): it stays on record with who and why,
  its number is never reused, and what the family owes is untouched.
- Migrations: `receivables` and `bankconnect` reference each other, so the order is `receivables 0002`, `bankconnect 0003`,
  `receivables 0003` (Django resolves it). `makemigrations` also proposes unrelated migrations for existing model drift in
  `students` and `academics`; those are not part of this work.
