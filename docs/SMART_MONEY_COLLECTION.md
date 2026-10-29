# Smart Money Collection (backend)

How a school collects fees from families through **its own** Paystack or Monnify account, and how SkulGen finds out what
was paid. This file says what is real, what is pending and how to run it. School fees are the **school's** money: SkulGen never
receives, holds or settles it. (What schools pay SkulGen - the SaaS subscription - is `apps/billing`, a different thing entirely.)

> **Smart Money Collection currently supports Paystack and Monnify for family collection accounts** (Family Payment Details).
> **Remita is reserved for SkulGen Mandates / Direct Debit and is not a Smart Money Collection provider.** The two are separate
> domains and are never mixed: see [Remita and Mandates](#remita-and-mandates-not-part-of-smart-money-collection) at the end.

```
School's own provider account -> school's own credentials -> SkulGen connector -> ONE active provider
   -> a family's collection account -> the provider moves the money -> signed provider event
   -> SkulGen reconciliation -> the family receivables ledger (canonical)
```

## What lives where

| App | Owns |
|---|---|
| `apps/bankconnect` | the provider connections (credentials, webhook, environment), the connector interface and the two adapters (Paystack, Monnify), provider events, matching and the review queue |
| `apps/receivables` | families, charges, adjustments, family credit, the family collection accounts and their statements - the canonical ledger |
| `apps/smartcollect` | policy, batches, approval, generation, provider switching (see the second half of this file) |

## Provider connections (`apps/bankconnect`)

`CollectionProviderConnection` (was `BankConnection`; the old name is an alias, the table was renamed in place by migration
`bankconnect 0004`, so no payment or reconciliation history moved or was lost). It holds only safe facts: provider, environment,
merchant name and masked reference, status, webhook status and when it was last verified, plus the **sealed** credential.

* The school enters credentials **its provider issued to it**: Paystack `secret_key`; Monnify `api_key`, `secret_key`,
  `contract_code`. There is no settlement account number, no account
  fingerprint and no "is this your school's account?" question.
* Credentials are sealed by the vault (`vault.py`, MultiFernet, `BANKCONNECT_SECRET_KEYS`, bound to school and connection so a
  copied ciphertext does not open). No serializer, log line, error message, audit detail or admin field can carry one; a provider's
  own error text is never copied, only SkulGen's words.
* A school may connect both providers (one live connection per provider per school, enforced by the database) but has exactly
  **one active collection provider** (also a database rule). Only Paystack or Monnify (or the development sandbox) can be the active
  one: the database refuses anything else, so it does not depend on the screen hiding it. It is chosen once with `activate`; any later change is a reviewed
  provider switch.
* A connected provider that has live family accounts, or is the active provider, can be neither disabled nor disconnected -
  nor can one a `smartcollect` generation batch is still issuing provider calls through (`has_in_flight_batch`), nor one
  either side of an open provider switch names (`has_pending_switch`): cutting either off mid-operation would strand calls
  already under way. None of this touches **history**: reviewing or reconciling an old transaction never looks at the
  connection's own status, so a disabled or disconnected connection's past payments stay exactly as reconcilable as before.
* The webhook address carries a random token (only its hash is stored). It is called **active** only after a verified event has
  actually arrived: never on setup.

### Who may do what (duties)

| Duty | Authority |
|---|---|
| `finance.collection_provider_manage` | connect, replace, test, disable providers; webhook; active provider; provider switches. The only authority that can put a provider secret into SkulGen |
| `finance.collection_policy_manage` | the school's collection policy and its overrides |
| `finance.collection_prepare` | the **maker** of a collection batch |
| `finance.collection_approve` | the **checker** of a collection batch |
| `finance.reconciliation` | decide what a payment under review means - assign, split, mark a duplicate or unrelated income, reverse, refund, reopen |

The proprietor holds all five. `finance.billing_authority` (what families owe) deliberately does **not** open provider secrets.
Seeing the review queue (`can_view_collections`, the owner, the Finance Office by role, or any of the five duties) is deliberately
**not** the same authority as deciding what is in it: an Accountant sees it by role alone, but changing a transaction needs the
`finance.reconciliation` duty explicitly, checked both in the view and again in `review.decide()` itself. The earlier
`finance.bank_connections` duty is honoured as provider authority only, so an assignment already made keeps working.

## The connectors (`apps/bankconnect/providers`)

One interface (`CollectionConnector`): `validate_credentials`, `get_merchant_profile`, `provision_family_collection_account`,
`get_collection_account`, `deactivate/reactivate/close_collection_account`, `verify_transaction`, `requery_transaction`,
`handle_webhook`. Each adapter declares its real capabilities and the rest of SkulGen reads the flags; calling what a provider
does not do raises `NotSupported`. Every adapter is written only against the provider's published documentation and tested with
the documented request and response shapes through a fake transport (`tests/fake_transport.py`).

| | Paystack | Monnify |
|---|---|---|
| Family collection account | Dedicated Virtual Account for a Customer | Customer Reserved Account |
| Static / dynamic | static, dynamic | static, dynamic |
| Deactivate / reactivate / close | deactivate and close (`DELETE`); no reactivate documented | close only |
| Customer KYC | no | **BVN or NIN required** |
| Webhook authenticity | `x-paystack-signature`, HMAC-SHA512 of the body with the secret key | `monnify-signature`, HMAC-SHA512 with the client secret - live only; sandbox is unsigned so every event is confirmed by requery |
| Webhook set up | in the dashboard | in the dashboard |

Pending for lack of official documentation or credentials (nothing is invented): Monnify's contract code is only proven when the
first account is made, and is not claimed as verified before then.

## Provider events and reconciliation

The public route `bank-webhooks/<provider>/<token>/`: the token finds the connection (an unknown one is a plain 404), the provider's
own authenticity check runs **before** anything is trusted or stored, the delivery is recorded by a hash so the same delivery is
never processed twice, and the record and the payments it carried are one database transaction. No provider is called while a
transaction is open.

A payment is matched to a family by **the account it was paid into** - deterministically. An account SkulGen has no record of is
kept, unmatched, for a person; it is never attached to a family by guessing from a narration or a sender's name. (The fuzzy
student-matching engine still serves payments from connections made by the earlier bank-account model; Smart Money Collection
payments never use it.) A payment into a settled, dormant or closed account that the provider confirms goes through the normal
pipeline; SkulGen invents no rule about it, and anything beyond what is owed becomes family credit.

## Webhook failure visibility and recovery

Every outcome of a delivery is now distinguished and recorded safely, never just allowed to raise an unhandled
error: an **unauthenticated** request (a bad signature) is audited (`webhook_rejected`) but never touches the
connection's own status, since noise or an attack must never be mistaken for evidence the school's own credential
is broken. An **authenticated event that failed somewhere after that** - the provider could not be reached, our
own secret storage failed to open, the body was unreadable, or something broke partway through recording the
payments - calls `provider_connections.record_failure()` (the same signal a failed manual connectivity test
already gave) and writes a `webhook_processing_failed` audit entry with one of SkulGen's own short, fixed codes,
never the provider's own message or anything from the request body. A later success (`record_success()`) clears
it. The Finance dashboard's connection health list (`summary.build()`'s `providers.connections`) and the
Collections screen's own "Activity" history both read from this, so a school that is not being heard from is
visible, not silent.

**What this does not, and should not, do:** it is not a retry queue. The whole attempt - the delivery record and
every payment it would have created - rolls back together on any failure, so the exact same delivery, retried
later, starts fresh rather than being half-remembered or skipped. Recovery from there is the **provider's own**
webhook retry policy (Paystack and Monnify both retry a non-2xx response on their own schedule, commonly for
around a day to a few days depending on the provider), plus `reconciliation.reconcile_pending()`'s own sweep,
which picks up anything ingested but not yet reconciled (for example after a crash between the two) regardless of
how long ago it arrived.

**Reviewed, as asked, before building anything new:** a dead-letter queue or a new background worker is **not**
introduced, because the need is not demonstrated for the common case - a transient failure (a brief provider
outage, a momentary vault error) is now visible, self-clears on the provider's own retry, and was the gap this
pass actually closes. Two genuine risks remain, neither solved by a queue on SkulGen's side:

1. **A webhook that never reaches SkulGen at all** - dropped in transit, or never sent because of a problem on
   the provider's own side before it even attempts delivery. Nothing is recorded, because nothing arrived: there
   is no `BankWebhookEvent` row, no audit entry, and no amount of retrying on SkulGen's end helps, since the
   failure is not SkulGen's to retry. The only way this is ever found today is a person noticing an expected
   payment is missing and asking Finance Office to look, which has no tool to directly ask the provider "did you
   send this?" - `verify_transaction()` already exists on both connectors (it is how Monnify's unsigned sandbox
   path confirms a payment today) but nothing staff-facing calls it for an arbitrary reference. A small,
   justified follow-up - not a queue, a single on-demand "check this reference with the provider" action for
   someone who already holds the reconciliation duty - would close this, but is not built here since it was not
   asked for in this pass.
2. **The provider's own retry window is exhausted before a prolonged SkulGen-side outage resolves** (secure
   storage down for longer than the provider keeps retrying, for example). Once the provider gives up, the same
   manual-verify gap in (1) is the only way back; a bank statement import (see below, design only) would also
   catch this, since the money still shows up in the school's own bank statement even when the webhook never
   does.

Both risks point at the same two follow-ups: a manual "verify with the provider" action, and bank statement
import as the ultimate backstop for whatever a webhook-only design can never fully guarantee. Neither is built in
this pass.

## Bank statement import (design only - not yet implemented)

Some schools receive payments straight into a commercial bank account with no Paystack or Monnify integration at
all, or want their bank statement as an independent check against what the webhook path already recorded. This
is a design for review, not code: nothing below exists yet, and the **critical rule** it is built around is that
**a CSV upload must never automatically become a provider-verified payment** - the whole design exists to make
double-crediting the same real transfer (once from a provider webhook, once from a statement line) structurally
hard to do by accident.

**Formats.** CSV only, to start: every Nigerian bank's own online banking already exports it, or can be coerced
to it from Excel. Not PDF statements - reliably parsing a bank's PDF layout is a much harder, error-prone problem
better deferred than guessed at. A school's columns are mapped once (date, narration, amount, and either a single
signed amount or separate debit/credit columns, since banks differ) and the mapping is remembered for its next
upload. UTF-8 (with BOM tolerance, since Excel exports it); a file that fails to decode is refused, never guessed at.

**Who may upload, who may confirm.** Uploading a file is a Finance Office task (the same people who already see
Collections, `can_view_collections`); it only ever creates **staging** rows (see below), so it changes nothing
real yet. Confirming a staged line as an actual payment - the step that can affect a balance - needs the same
`finance.reconciliation` duty this pass just gave its own dedicated check (see "Reconciliation permissions"
above): looking at a statement is not authority to decide what it means, for exactly the same reason viewing the
provider review queue is not.

**Validation and safe processing.** A size cap (a school-term's worth of transactions is a few thousand rows, so
a generous but bounded limit) and a row cap. Every row is validated in a dry run first - date parses, amount
parses, direction is unambiguous - before anything is written: the admin sees a preview (how many rows, how many
failed to parse and why) and explicitly confirms the upload before it is committed, mirroring the
preview-then-commit pattern `apps.smartcollect`'s own batches already use. Committing writes in bounded chunks,
each its own transaction, not one lock held over a whole file.

**Staging, not `BankTransaction`, until confirmed.** An imported row lands in a new model of its own (something
like `ImportedStatementLine`: school, date, amount, direction, narration, reference if the bank gives one, a
content hash, and a status - `staged`, `confirmed`, `discarded`) rather than directly in `BankTransaction`. This
is deliberate, not incidental: a status flag on the existing model is one query away from being forgotten in some
future report; a separate table structurally cannot leak into `summary.build()`'s counted totals, the review
queue, or a family's statement, because nothing reads it from there at all. Only `confirm` promotes a staged line
into a real `BankTransaction` (`provider="statement_import"` - a new code, deliberately kept out of
`COLLECTION_PROVIDER_CODES`, so it can never be an active provider or look like a live connection), after which it
runs through the **existing, unmodified** matching, allocation, credit and reversal machinery - no second
reconciliation engine, exactly as asked.

**Duplicate identification - two different duplicates.**

* *The same statement line imported twice* (re-uploading a file, or two uploads with an overlapping date range):
  caught by a hash of (school, date, amount, narration) at staging time, the same idea `BankWebhookEvent`'s
  payload hash already uses for webhook deliveries.
* *The same real transfer SkulGen already has from a provider webhook*: this is the critical rule's actual
  target, and it is never resolved automatically. At confirm time, a staged line with a close amount and date is
  checked against existing `BankTransaction` rows for the school; if one plausibly matches, the reviewer sees it
  and is asked directly - "this looks like the settlement of an already-recorded payment; confirming this
  separately could double-count it" - and must explicitly choose: confirm it anyway as a genuinely separate
  payment, or discard it as the same money already recorded. Nothing is ever promoted past a plausible match
  without that explicit choice.

**Audit and correction.** Every upload and every confirm/discard decision is an audited event (who, the file,
how many rows, which were promoted), the same pattern `BankAuditEvent`/`ReconciliationDecision` already use
everywhere else in this app. Once a line is promoted it is an ordinary `BankTransaction`: reassigning, splitting
or reversing it afterwards needs no new code at all, since the existing review workflow already does this for
any transaction regardless of which provider code it carries.

**Staying distinguishable.** `provider="statement_import"` is shown wherever a payment's provider is shown today
(the same way a sandbox/test transaction already carries its own label), and `summary.build()` would report a
confirmed import's amount in its own line, never silently folded into the provider-webhook total - the same
"never silently combine two sources" rule this pass's Finance dashboard and Parent Finance work already
established for the manual ledger and Smart Money Collection. School isolation needs no new pattern: every
staging row and the view that reads it is scoped by `school`, exactly like everything else here.

Not implemented until this design is reviewed and approved.

## Legacy data

The earlier bank-account connections and everything recorded against them (transactions, allocations, decisions) stay exactly as
they were. Family accounts recorded by hand are marked `legacy_manual`; recording one is restricted to provider managers and is not
part of the normal journey. Where a family held several live accounts, the oldest stays live and the others were **closed, not
deleted**, so the one-live-account-per-family-per-school rule could be enforced without losing history.

## Policy, batches, approval, generation (`apps/smartcollect`)

### The collection policy

`SchoolCollectionPolicy` is the school's default, typed field by field: account type (`static` / `dynamic`), how long a static account is
reused (one term, a number of terms, one session, a number of sessions, until a date, indefinitely, until replaced), what happens when a
family has paid (close immediately, dormant immediately, wait then dormant, wait then close, leave it for a person, let the provider decide),
the waiting period (**chosen by the school; no default**), how earlier balances are treated (carry forward, current term only, choose which),
how a family that still owes for an earlier term is treated (exclude, include only with an override, include, approve each by hand), what
happens to old accounts when the provider is switched, and when an override needs a reason (never / always / for sensitive ones).

Layers over it, **nearest wins**: family > batch > term > session > school default. An override holds only the fields it changes;
everything else keeps inheriting, and a resolved policy always says where each value came from (`school`, `session`, `term`, `batch`,
`family`). An override can expire (once, end of term, end of session, a date, until removed), is audited, and is **never edited or deleted**:
replacing or removing it leaves the old one as history. A family, term or batch can never choose a different provider.

### The batch

`prepare -> preview -> select -> (override, choose balances) -> submit -> approve/reject -> start -> generate -> (retry)`

* **Preview.** Every active family with a student, worked out from the ledger: what it owes now, what it owes for earlier terms, its
  eligibility bucket and why, what its account would be asked to collect, whether it already has an account (kept, replaced, or in
  conflict), and whether the provider has what it needs about the payer (Monnify: a BVN or NIN, recorded write-only and sealed). The
  preview reads only: no provider is called and the ledger is never touched (arrears carried into a target are still owed as before).
* **Snapshot.** The batch has a `version` and a `snapshot_hash` over exactly what would be generated. A screen sends the version it was
  looking at; a stale one gets `409 stale_preview`.
* **Maker and checker.** The maker holds `finance.collection_prepare`, the checker `finance.collection_approve`. Nobody who prepared,
  changed or submitted a batch can approve or reject it (the service says so, and so does the database). A rejection needs a reason and
  returns to the maker with the whole history kept. Approval is for one hash; if anything that matters changes (a payment, a charge, the
  policy, a family override, an account made elsewhere, the provider), the approval is **withdrawn** and the batch goes back to its maker.
* **Generation.** Only an approved batch can start, and only for the exact approved snapshot. Starting queues one `ProviderJob` per
  family in the same transaction as the decision; a worker makes the provider call later, **never inside a database transaction**.
  Each family's idempotency key and provider reference are deterministic, the connectors look before they create, and the account is
  recorded once by key, so a repeat, a double click or two workers racing can never make a second account. A provider that does not answer
  is tried again after a growing wait; after the last attempt the family is marked failed for a person to retry.
* **Partial failure.** Successes stay successes ("Generated with errors: 590 successful, 10 failed"). Failed families can be retried
  together or one by one. A retry under an **unchanged** snapshot reuses the approval; if the snapshot changed, the approval is withdrawn
  and the batch goes back for a fresh one, with every success frozen: a generated family is never regenerated.
* **Export.** The preview as a PDF or an Excel workbook (`export/?type=pdf|xlsx`), written with the standard library, carrying the batch
  version and fingerprint. It reads what is stored and calls no provider.

Running the queue: `python manage.py run_collection_jobs` (once, or `--loop` as a worker). It also ends grace periods that are over and
looks again at planned provider switches. Where no worker runs, `SMART_COLLECTION_INLINE_JOBS` (default on) lets the server drain a
bounded slice itself (`SMART_COLLECTION_INLINE_SECONDS`) when a batch is started and while its progress is watched; turn it off where a
worker runs.

### After a family has paid

The settlement action in the policy decides. `close` and `wait then close` start **closing** the account: it stays the family's live
account and keeps receiving while a queued call retires it at the provider (Paystack deactivates the dedicated account, Monnify
deallocates the reserved account). `dormant` and `settled` are SkulGen states and call no provider. Nothing is
ever deleted, and a payment the provider confirms into an account in **any** state goes through the normal pipeline (anything beyond what
is owed becomes family credit; nothing is sent to review for the state alone).

### Switching provider

`scheduled -> ready_to_switch -> applied` (or `cancelled` / `failed`). A switch becomes **ready** when its date has come and nothing
stands in the way (the new provider connected, no batch generating); it is **never applied by itself**. A person with authority over the
providers reviews it (current and target provider, families and accounts affected, what is owed, readiness, warnings) and applies it: the
target becomes the one active provider in a single transaction, batches prepared for the old provider are withdrawn, and the old accounts
are handled as the school's switch policy says. Their history stays, and the old provider stays connected while any of its accounts is
live, because payments still arrive through it. A family never has accounts with two providers.

### API (`/api/v1/schools/<school>/collections/...`)

`dashboard/`, `policy/` (GET, PATCH), `policy/effective/`, `policy/overrides/` (+ `<id>/remove/`), `switches/` (+ `<id>/`,
`<id>/apply/`, `<id>/cancel/`), `batches/` (+ `<id>/`, `items/`, `events/`, `export/`, `progress/`, `failed/`, and the actions `preview`,
`selection`, `title`, `policy`, `submit`, `approve`, `reject`, `cancel`, `start`, `retry`, and per family `items/<id>/override`,
`override-clear`, `arrears`), `families/<id>/payer-identity/`; and, in receivables, `families/<id>/collection-accounts/` (a family's
account history, including which batch made each account). Refusals carry a stable `code`; `409` means "what you were looking at is out of
date". No response carries a credential or an identity number.

### Pending (nothing invented)

* Monnify's contract code is only proven when the first account is made, and is not claimed as verified before.
* Whether a provider-side account is "dormant" is SkulGen's own flag; no provider documents a way to refuse transfers to a reserved
  account without closing it, so none is called for it.
* Bank statement import: designed above, not built. The same for a bank-data aggregator (Mono, Okra, OnePipe or similar) -
  deliberately deferred, no commercial dependency added and no credentials requested.
* A staff-facing "check this reference with the provider directly" action, for a payment suspected missing because its webhook
  never arrived at all: `verify_transaction()` already exists on every connector but nothing calls it on demand yet. See "Webhook
  failure visibility and recovery" above.
* A parent-facing way to supply a BVN/NIN; today the school records it (write-only) for the payer.

## Remita and Mandates (not part of Smart Money Collection)

Remita is **not** a Smart Money Collection provider. An earlier version treated a Remita payment reference (an RRR invoice) as though
it were a family's collection account; that was removed by a product decision, and no RRR is a hidden third mechanism. Remita is
reserved for the separate **Mandates & Direct Debit** feature (Remita and Lendsqr), described in [MANDATES_DIRECT_DEBIT.md](MANDATES_DIRECT_DEBIT.md):

```
School -> own Remita relationship -> mandate integration -> payer authorises a mandate
   -> direct-debit instruction -> Remita confirms payment -> the receivables ledger (canonical)
```

What that means in the code, and what is enforced by the **server** rather than by the screens:

* The provider registry (`providers/registry.py`) lists only `paystack` and `monnify` (and the sandbox, in development). There is no
  Remita adapter, so nothing can connect it: the connect call answers `unknown_provider`.
* Only a Paystack or Monnify (or sandbox) connection can be the school's active provider: the activate call refuses anything else and a
  database rule (`active_provider_is_a_collection_provider`) refuses it even if the application did not.
* A collection batch can only be prepared for such a connection (it takes the school's active provider; the batch model refuses any
  other), a family collection account can only be made by one (the account model refuses any other, and recording a Remita reference by
  hand is refused with `provider_not_supported`), and a provider switch is only ever between Paystack and Monnify (a switch that involves
  anything else is blocked and can never be applied). The webhook route answers 404 for a `remita` address.
* The summary counts only Paystack and Monnify as connected providers. Payments that were already recorded under an earlier Remita
  connection stay in the ledger and keep being read deterministically; nothing new is read from Remita.
* Removed with it, because they existed only for Remita: the "make the account for an amount" provider trait (`requires_amount`), the
  `COLLECTION_REMITA_LIVE_BASE_URL` setting, the `Ok` / `Not Ok` webhook acknowledgement, and the direct-debit capability flag (mandates
  are a different domain, not a capability of a family-account provider).
* The old RRR adapter was written against Remita's invoice API, which is not the direct-debit API, so none of it is carried into mandates. Mandates &
  Direct Debit (`apps/mandates`, see [MANDATES_DIRECT_DEBIT.md](MANDATES_DIRECT_DEBIT.md)) is designed and written afresh, from Remita's official Direct Debit
  documentation and Lendsqr's, with its own connections (never `is_active_provider`, never in the "one active collection provider" rule), its own duties
  (`finance.mandate_provider_manage`, `finance.mandate_manage`, `finance.mandate_prepare`, `finance.mandate_approve`, separate from the four collection
  duties), audit, idempotency, maker/checker and duplicate protection. Receivables stay the accounting truth: a debit is tied to the canonical receivable
  position, never to a client-supplied amount, and no mandate table keeps a second school-fee balance. The old adapter remains in git history for reference only.

Data already made under the earlier design is handled explicitly and deletes nothing (`bankconnect 0005`, `smartcollect 0002`; run them
over a copy first if a school ever used Remita):

| Left behind | What the migration does |
|---|---|
| a Remita connection | disconnected; its sealed credential and callback address are dropped; it stops being the active provider. No other provider is switched on in its place: the school chooses |
| a family's Remita account | closed (or marked failed if it never had a reference), so the family can be given a Paystack or Monnify account. Nothing is asked of Remita: an RRR it already issued is not cancelled by SkulGen |
| a batch that had not made accounts | cancelled; a batch that was generating stops and keeps the families that succeeded; finished batches stay as they were |
| a planned switch involving Remita, and queued Remita calls | the switch is cancelled and the calls are failed |
| payments and audit rows | untouched |

Each change is written to the audit trail. On a school that never used Remita the migrations change nothing.

## Tests

`apps.bankconnect` (connectors against the documented shapes, connection API, permissions, secrecy, webhooks, reconciliation),
`apps.receivables`, and `apps.smartcollect` (that only Paystack and Monnify can be connected, made active, batched, switched between or
given family payment details, with Remita refused by the server, and the migrations that retire what Remita left run over real rows;
policy layers, expiry and history; preview, eligibility and arrears; maker-checker and
staleness; generation, idempotency, partial failure and retry; lifecycle; switching; the job queue and that no provider is called inside a
transaction; identity; exports; the API). No test calls a real provider: connectors are exercised through a fake transport that answers as
the provider's documentation says it does, and the sandbox connector stands in for a provider everywhere else.
