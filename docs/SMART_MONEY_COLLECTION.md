# Smart Money Collection (backend)

How a school collects fees from families through **its own** Paystack or Monnify account, and how SchoolOS finds out what
was paid. This file says what is real, what is pending and how to run it. School fees are the **school's** money: SchoolOS never
receives, holds or settles it. (What schools pay SchoolOS - the SaaS subscription - is `apps/billing`, a different thing entirely.)

> **Smart Money Collection currently supports Paystack and Monnify for family collection accounts** (Family Payment Details).
> **Remita is reserved for SchoolOS Mandates / Direct Debit and is not a Smart Money Collection provider.** The two are separate
> domains and are never mixed: see [Remita and Mandates](#remita-and-mandates-not-part-of-smart-money-collection) at the end.

```
School's own provider account -> school's own credentials -> SchoolOS connector -> ONE active provider
   -> a family's collection account -> the provider moves the money -> signed provider event
   -> SchoolOS reconciliation -> the family receivables ledger (canonical)
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
  own error text is never copied, only SchoolOS's words.
* A school may connect both providers (one live connection per provider per school, enforced by the database) but has exactly
  **one active collection provider** (also a database rule). Only Paystack or Monnify (or the development sandbox) can be the active
  one: the database refuses anything else, so it does not depend on the screen hiding it. It is chosen once with `activate`; any later change is a reviewed
  provider switch.
* A connected provider that has live family accounts, or is the active provider, can be neither disabled nor disconnected.
* The webhook address carries a random token (only its hash is stored). It is called **active** only after a verified event has
  actually arrived: never on setup.

### Who may do what (duties)

| Duty | Authority |
|---|---|
| `finance.collection_provider_manage` | connect, replace, test, disable providers; webhook; active provider; provider switches. The only authority that can put a provider secret into SchoolOS |
| `finance.collection_policy_manage` | the school's collection policy and its overrides |
| `finance.collection_prepare` | the **maker** of a collection batch |
| `finance.collection_approve` | the **checker** of a collection batch |

The proprietor holds all four. `finance.billing_authority` (what families owe) deliberately does **not** open provider secrets. The
earlier `finance.bank_connections` duty is honoured as provider authority only, so an assignment already made keeps working.

## The connectors (`apps/bankconnect/providers`)

One interface (`CollectionConnector`): `validate_credentials`, `get_merchant_profile`, `provision_family_collection_account`,
`get_collection_account`, `deactivate/reactivate/close_collection_account`, `verify_transaction`, `requery_transaction`,
`handle_webhook`. Each adapter declares its real capabilities and the rest of SchoolOS reads the flags; calling what a provider
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

A payment is matched to a family by **the account it was paid into** - deterministically. An account SchoolOS has no record of is
kept, unmatched, for a person; it is never attached to a family by guessing from a narration or a sender's name. (The fuzzy
student-matching engine still serves payments from connections made by the earlier bank-account model; Smart Money Collection
payments never use it.) A payment into a settled, dormant or closed account that the provider confirms goes through the normal
pipeline; SchoolOS invents no rule about it, and anything beyond what is owed becomes family credit.

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
deallocates the reserved account). `dormant` and `settled` are SchoolOS states and call no provider. Nothing is
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
* Whether a provider-side account is "dormant" is SchoolOS's own flag; no provider documents a way to refuse transfers to a reserved
  account without closing it, so none is called for it.
* A parent-facing way to supply a BVN/NIN; today the school records it (write-only) for the payer.

## Remita and Mandates (not part of Smart Money Collection)

Remita is **not** a Smart Money Collection provider. An earlier version treated a Remita payment reference (an RRR invoice) as though
it were a family's collection account; that was removed by a product decision, and no RRR is a hidden third mechanism. Remita is
reserved for a separate **Mandates / Direct Debit** feature, which is **not started**:

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
* The old RRR adapter was written against Remita's invoice API, which is not the direct-debit API, so none of it is carried into
  mandates: a mandate integration is designed afresh, from Remita's official direct-debit documentation, with its own connection
  (never `is_active_provider`, never in the "one active collection provider" rule), its own duties (for example
  `finance.mandate_manage`, `finance.mandate_prepare`, `finance.mandate_approve`, given separately from the four collection duties),
  audit, idempotency, maker/checker and duplicate protection. Receivables stay the accounting truth: a debit is tied to the canonical
  receivable position and the school's policy, never to a client-supplied amount, and no mandate table keeps a second school-fee balance.
  The adapter remains in git history (the commit before this one) for reference only.

Data already made under the earlier design is handled explicitly and deletes nothing (`bankconnect 0005`, `smartcollect 0002`; run them
over a copy first if a school ever used Remita):

| Left behind | What the migration does |
|---|---|
| a Remita connection | disconnected; its sealed credential and callback address are dropped; it stops being the active provider. No other provider is switched on in its place: the school chooses |
| a family's Remita account | closed (or marked failed if it never had a reference), so the family can be given a Paystack or Monnify account. Nothing is asked of Remita: an RRR it already issued is not cancelled by SchoolOS |
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
