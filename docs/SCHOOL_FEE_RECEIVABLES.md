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
  person closed; never merges two families;
- reports conflicts, students with no usable reference, and sibling hints for a person to look at;
- with `--singletons`, gives each remaining student a family of their own (never merged), which a person can join later;
- is safe to run again: it finds the family it made by its `origin` and extends it.

The old fields stay for compatibility. New finance work uses the canonical Family.

## Fee schedules

Only a billing authority drafts, edits and publishes. **Publishing** fixes the schedule and raises one charge per
student per item (or instalment) for everyone it applies to.

- **Applicability comes from the academics app** (`EnrollmentAcademicContext`), never from class names typed by a client.
  A billable student the academics records cannot place for the session is **reported and not charged**; so is a
  student with no family. A later `refresh` charges them once fixed (and any student who joined after publication). It
  only ever creates what is missing.
- Corrections after publication are explicit: `retire` (with a reason; charges stay), `clone` (a new draft), `adjust`,
  `void`, `void-charges`. A schedule that **replaces** another cannot be published until the original is retired, and
  never charges a student again for an item the original already charged (that is an adjustment, not a second charge).
- `preview` shows who would be charged and how much without charging anyone.

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

Provider-agnostic. After **every** ledger change: family owes something -> the account is **ACTIVE**; owes nothing ->
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

- **Families** (operator): `families/` (GET search, POST create), `families/<id>/`, `.../rename|add-student|remove-student|link-guardian|set-status/`, `.../receivables/`, `.../statement/` (GET, derived), `.../statements/` (GET, POST issue), `.../credit/`, `.../credit/refund/`, `.../payments/`, `.../collection-accounts/` (GET, POST register), `families/unassigned-students/`, `families/bridge/` (GET report, POST apply); `collection-accounts/<id>/suspend|reinstate|close|mark-provisioned/`.
- **Fee schedules** (read: operator; change: billing authority): `fee-schedules/`, `.../<id>/`, `.../preview/`, `.../rename|publish|refresh|retire|clone|void-charges/`, `.../items/`, `.../items/<item>/update|remove/`.
- **Charges and decisions**: `charges/` (filters and paging), `charges/<id>/`, `charges/<id>/adjust|void/` (billing authority), `adjustments/` (history), `adjustments/<id>/reverse/` (billing authority).
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

`manage.py test apps.receivables` (293 tests) covers: authority (every role, revoked/pending duties, other schools);
families and the bridge; fee schedules (every rejection, freezing, applicability, instalments, idempotent publishing);
adjustments, reversals, voids and credit release; credit; allocation policy, reversals and corrections; collection account
lifecycle; bank integration, including the real public webhook route into a family account (a repeated or forged
delivery pays nothing twice); concession integration; statements; notifications; every API endpoint against a second
school; and a **seeded random sequence of every operation with the whole ledger's invariants and conservation of money
checked after each step**. Run once at larger scale (60 seeds x 40 steps) with no violation.

## Assumptions and what is deliberately not here

- **Direct-debit mandates** (Remita, Lendsqr) are not implemented. The ledger answers what a mandate integration will ask:
  who owes (`family_position`), how much, what is due (`outstanding`, `overdue`), what has been paid, what credit exists,
  and whether anything is collectible (`collectible`).
- **No provider adapter issues family accounts yet.** `collection_accounts.register` records the account a provider has
  given a family; issuing one is a provider connector's job (and needs that provider's documentation).
- Currency is NGN only.
- A student removed from a family leaves the charges already raised with the family that was billed; a family set inactive
  raises no new charges but its existing ones stand. Merging two families is not built.
- A statement's `void` status exists; there is no endpoint to void one.
- The app (Flutter) is not changed here except offering the duty. In particular the Parent Finance screen still shows the
  child's internal id as an "account number" until it is moved to `me/families/`.
- Migrations: `receivables` and `bankconnect` reference each other, so the order is `receivables 0002`, `bankconnect 0003`,
  `receivables 0003` (Django resolves it). `makemigrations` also proposes unrelated migrations for existing model drift in
  `students` and `academics`; those are not part of this work.
