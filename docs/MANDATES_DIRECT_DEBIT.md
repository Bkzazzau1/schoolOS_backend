# Mandates & Direct Debit (backend)

How a school collects fees from families through a **direct-debit mandate** the payer authorises, using **its own** Remita or Lendsqr
account. This file says what is real, what is pending and how to run it. School fees are the **school's** money: SchoolOS never
receives, holds or settles it, and it is not a bank or a debit scheme. The provider executes the debit.

```
School's own Remita / Lendsqr account -> the school's credentials (sealed) -> SchoolOS connector
   -> a payer authorises a mandate on their own bank account -> the provider activates it -> debit-ready
   -> a maker prepares a debit, a different checker approves exactly what they saw
   -> the ledger is read again -> the provider is asked (through a job, never in a transaction)
   -> the provider confirms -> ONE receivables payment, settled by the ordinary allocation -> the family's balance
```

## Not Smart Money Collection

There are two payment paths and they share **accounting truth** (the receivables ledger) and nothing else.

| | Smart Money Collection | Mandates & Direct Debit |
|---|---|---|
| Providers | Paystack, Monnify | Remita, Lendsqr |
| Family gets | Family Payment Details (a virtual account) | a mandate on their own bank account |
| Who moves the money | the parent, by transfer | the provider, on an approved debit instruction |
| Provider connections | `bankconnect.CollectionProviderConnection`, exactly one **active** | `mandates.MandateProviderConnection`, **both** may be connected, **no** active provider |
| Batches / jobs / policy | `apps/smartcollect` | `apps/mandates` (its own tables) |
| Duties | `finance.collection_*` | `finance.mandate_*` |

Neither Remita nor Lendsqr is a Smart Money Collection provider (the collection registry refuses them), and Paystack and Monnify cannot
be connected for mandates. A database rule on each side enforces it.

## What lives where

`apps/mandates` (new): `constants`, `models/` (connection, mandate, debit, sandbox), `permissions`, `vault`, `identifiers`, `audit`,
`connections` (the provider connection lifecycle), `mandates` (start, readiness, primary), `consent`, `mandate_provider` (everything between
SchoolOS and the provider over a mandate's life), `evaluation` (what could be debited), `debit_batches` (maker-checker), `execution` (sending
debits safely), `settlement` (the ledger bridge), `jobs`, `webhooks`, `notifications`, `serializers`, `views_*`, `urls`, the providers under
`providers/`, and the `run_mandate_jobs` command.

## Providers, and what is verified

Each connector is written **only** against the provider's own published documentation. Where the documentation is silent, the operation is
refused with `pending_documentation`; nothing is guessed.

### Remita (Direct Debit, "For Non-Financial Institutions")

Source: Remita's published API documentation (api.remita.net): Generate Mandates, Activate Mandate, Mandate Operations, Check Status and
Webhook Notifications, with its Fields and Status Codes.

* Credentials: `merchant_id`, `service_type_id`, `api_key`, `api_token`.
* A **variable** mandate is set up (`mandateType` `DD`: a maximum amount and a maximum number of debits); the hash is SHA-512 of
  merchantId + serviceTypeId + requestId + amount + apiKey; dates are `dd/MM/yyyy`.
* The payer activates either with a **bank one-time password entered in SchoolOS** (`requestAuthorization` / `validateAuthorization`, headers
  MERCHANT_ID, API_KEY, REQUEST_ID, REQUEST_TS, API_DETAILS_HASH = SHA-512 of apiKey + requestId + apiToken; only the banks Remita lists, personal
  accounts only) or by **printing and signing the mandate form** Remita gives (the print address is built as documented).
* `mandate/status` says whether it is active; `mandate/stop` ends it. **Remita documents no pause**, so it is cancel only (SchoolOS can still
  stop debiting a mandate itself: see Suspending).
* A debit is `mandate/payment/send` with the payer's funding account and bank code (so the sealed account number is kept for as long as the
  mandate lives), and `mandate/payment/status` asks about it by the same request id. Remita's own status code says whether it is paid: **only
  `01`** is. `069`, `070`, `071`, `072` are pending; `051`, `107`, `034`, `035`, `061`, `062`, `063`, `066`, `067`, `073` are failures with a reason;
  `074` means Remita has no record of the request; anything else is **unknown** and never settles.
* Notifications (`ACTIVATION`, `DEBIT`) carry **no documented signature**, so a body is never believed: it names what to ask Remita about and
  Remita's answer decides. The reply is `OK`.
* The maximum is the most that can be debited in a **calendar month** (with a count of debits); SchoolOS counts what was already debited this month.
* Remita's documentation gives only its **demo** host. The live host is not in it, so an operator sets `MANDATES_REMITA_LIVE_BASE_URL` (and
  `MANDATES_REMITA_LIVE_FORM_BASE_URL` for the printable form) and a live connection is refused until then. Remita also requires a **UAT** with it
  before go-live. Credentials are checked with a status query for a mandate that cannot exist (documented codes: `013`/`020` bad credentials, `074`
  no record, which means the hash was accepted); the API token is only proven the first time a payer activates with a one-time password.
* The old RRR invoice adapter is **not** used: an RRR is not a mandate.

### Lendsqr (Adjutor Developer API)

Source: the Developer APIs pages at docs.lendsqr.com and the Adjutor API reference they point to (api.adjutor.io).

* Every call is `https://adjutor.lendsqr.com/v2/...` with `Authorization: Bearer <API key>`. **Test and Live mode use the same key and base URL**: the
  mode is a toggle on the app in the Lendsqr admin console, so SchoolOS cannot see which one a key is in and records what the school declares.
* `GET /direct-debit/banks` gives the banks, each with its activation amount and the NIBSS account the payer transfers it to.
  `POST /customers/direct-debit-mandates` makes a mandate for a Lendsqr **customer** (`user_id`), an account and a maximum per debit.
  `GET /customers/:id/direct-debit-mandates` is how status is asked for; `PATCH .../activate`, `.../deactivate`, `.../cancel` change it.
* **The payer must already be a customer in the school's own Lendsqr organisation.** Creating one needs BVN, date of birth, address and
  documents (lender KYC), which SchoolOS neither collects nor sends; the payer's Lendsqr customer id is entered when the mandate is started
  and remembered for that payer's next one. Without it no mandate is made.
* Activation is a **transfer** (usually N50, N100 at some banks) from the account to a NIBSS account, within Lendsqr's activation window
  (currently **168 hours**, after which it is cancelled). The instructions shown to the payer come from Lendsqr's bank list, not from SchoolOS code.
* The mandate is then *activated* but "isn't yet available for debit": NIBSS setup follows and takes "up to 2 hours". SchoolOS keeps
  **activated** (`pending_provider_setup`) and **debit-ready** (`active`) apart. The window and the setup time are **Lendsqr's rules and live in
  configuration** (`MANDATES_LENDSQR_ACTIVATION_WINDOW_HOURS`, `MANDATES_LENDSQR_DEBIT_SETUP_MINUTES`), never in business code, and the provider's
  own dates are used whenever it gives them. Lendsqr's pages have carried differing guidance on the window, so it is a setting, not a constant.
* **What is NOT documented: sending a debit on a mandate, asking about one, or a callback event.** Lendsqr's help pages show a "Trigger debit"
  action in the admin console, and its API has "Repay customer loan", which debits against a loan booked in Lendsqr's own lending ledger. Neither
  is a way to send an approved school-fee debit without a second ledger. So `create_debit` / `get_debit_status` are refused with
  `pending_documentation`: **a Lendsqr mandate can be made, activated and watched, but SchoolOS does not debit it until Lendsqr documents how.**
  A Lendsqr mandate's family shows as "Provider cannot debit yet" in a batch and cannot be selected.
* **Eligibility.** Lendsqr states that live use needs the organisation to be licensed as a lender (or otherwise legally entitled) and to have
  passed its KYC. SchoolOS never claims a school is eligible: a **live** Lendsqr connection is refused until an operator has confirmed the position
  and switched `MANDATES_LENDSQR_LIVE_ENABLED` on. Test mode is unaffected. Balance lookup and other borrower-data services are **not** used.
* Lendsqr amounts are sent as naira decimals (its Repay example shows `100000.50`); its reference does not state the unit for the mandate
  amount, so this is the one assumption made, and the first live mandate confirms it.

### The development sandbox

`MANDATES_ENABLE_SANDBOX` (defaults to `DEBUG`) offers a synthetic provider with every capability, deterministic mandates, idempotent debits by
reference, an OTP (`1234`), and fault injection (a debit whose answer is lost after it was carried out, one that never arrived). Its data is flagged
as test data and never offered in production. It is what the end-to-end tests run on, beside scripted Remita and Lendsqr servers.

## Duties

`finance.mandate_provider_manage`, `finance.mandate_manage`, `finance.mandate_prepare` (the maker), `finance.mandate_approve` (the checker).
The owner holds all four and can give any to a trusted person; being an Accountant, holding a Smart Money Collection duty, or holding
`finance.billing_authority` is **not** enough, and `mandate_provider_manage` (provider secrets) is not `mandate_manage` (mandates).

## Connections and secrets

A `MandateProviderConnection` holds only safe facts and the **sealed** credential. Sealing uses the same vault and key ring
(`BANKCONNECT_SECRET_KEYS`) as the rest of SchoolOS, with a context of its own, so a blob copied to another row, another school or another part of
SchoolOS will not open. No serializer, audit detail, log line or error carries a credential or an account number; a provider's own error text is never
copied. `manage.py reseal_bank_credentials` re-encrypts mandate credentials and payers' bank details too (rotate: new key first, run it, then drop the old key).

A school may connect **both** providers at once. Each mandate records the connection it was made under for good; there is no `is_active_provider`.
A connection with live mandates can be neither disabled nor disconnected.

## A payer's mandate

`DirectDebitMandate` belongs to a **family** and to a **payer** (a guardian of that family) and records its provider connection. It is a payment
rail, **not a fee balance**: nothing on it says what a family owes.

* **The bank account** is never in a readable column: a bank, a masked number and a keyed fingerprint (a duplicate mandate on one account is refused;
  the fingerprint differs between schools). The full number is sealed and kept **only** while the provider needs it again to debit (Remita) and removed
  once activated (Lendsqr, sandbox) or when the mandate ends. It is never returned to the app.
* **States**: `draft`, `pending_consent`, `pending_activation`, `activating`, `pending_provider_setup`, `active` (= debit-ready), `suspended`,
  `cancelled`, `expired`, `failed`; the provider's own word and code are kept beside them.
* **Debit-ready is stricter than activated**: the provider must say it can be debited, the payer's consent must be on record, the mandate must be
  inside its dates, the school's connection must be working, and the provider must be able to send a debit through SchoolOS.
* **Consent is first-class and never given by staff.** Either the payer, signed in as themselves, reviews the exact words (versioned, with their hash kept)
  and authorises in the app (the provider is asked only then), or the provider's own authorisation (a bank OTP, a signed form, an activation transfer) is
  the evidence and SchoolOS keeps the provider's reference. The database refuses a usable mandate without consent. A payer can always cancel.
* **One primary** live mandate per family (a database rule); a family may have several.
* **Suspending**: through the provider where it can pause a mandate (Lendsqr); otherwise SchoolOS itself stops debiting it (Remita) and says so, and the
  provider still calling it active does not undo that.
* **Expiry** and failure are reported by the provider and never revive a mandate.

## A debit

1. **Prepare** (maker): choose a session and term. The preview reads the ledger and the mandates only: no provider is called and no ledger entry is made.
   Each family shows its outstanding, what could be collected (less any family credit), the mandate's limit, the proposed debit and **exactly how it would
   pay the family's charges** (in payment order). Nothing is selected until a person selects it.
2. **The ledger decides the amount.** A mandate authorises a mechanism; its maximum is a ceiling, never a debit. A person can only **lower** an amount.
3. **Submit** for the exact fingerprint they saw; a stale preview is refused.
4. **Approve / reject** (checker): a different person, for the exact snapshot; a rejection needs a reason and history is append-only. The maker, and
   anyone who prepared, changed or submitted the batch, can never approve it (service and database).
5. **Any material change withdraws the approval**: a balance, the mandate (cancelled, suspended, no longer debit-ready, a new limit), the provider
   connection, an amount, the selection. The batch goes back to its maker.
6. **Start**: every selected debit is queued in the same transaction as the decision. Debits go through `MandateProviderJob`s and the provider is asked
   with no transaction open (`manage.py run_mandate_jobs`, or a bounded inline drain where no worker runs).
7. **Just before the provider is asked**, the ledger, the mandate and the connection are read again. If anything approved changed, **nothing is sent**: the
   debit is stopped (`changed_since_approval`) and needs a fresh review. It is never quietly shrunk and executed as a different debit.
8. **The intent is written first** (`MandateTransaction`, committed), then the provider is asked, then the answer is written.
9. **An unknown outcome is asked about, never repeated.** A timeout leaves the debit `UNKNOWN`; the provider is asked about the same reference until it says.
   Only "I have no record of that request" makes it safe to send again, with the same reference. A worker that died after writing the intent asks before it sends.
   A debit still unknown after every attempt is flagged for a person and never sent again.
10. **Settlement**: only a debit the provider confirms is put in the ledger, exactly once (see below). Pending, failed and unknown debits change nothing.
11. **Partial failure** keeps the successes. **Retry** re-sends failed debits under the **same approval** only if every approved fact is unchanged (a new
    reference for the deliberate retry); a changed one needs a fresh maker and checker, and an unknown one is never retried.

## Receivables integration

There is no second ledger and no second allocation engine. A confirmed debit becomes **one canonical receivables payment** (`BankTransaction`, marked
`direct_debit`, matched, known to belong to its family for certain, with no collection-provider connection: migration `bankconnect 0007` makes the
connection optional **only** for that) and is put towards charges by the **same** `allocation.allocate` every payment uses, limited to the charges
the approved debit was for (`only_receivables`), with anything beyond what is owed held as **family credit** rather than lost (which pre-execution
freshness makes rare). A provider **reversal or refund** takes the payment back out with the same release mechanism and is recorded; nothing is deleted.
Smart Money Collection's own payment list, review queue and summary exclude direct-debit payments (they have no connection); the family's payment history,
statement and ledger include them.

## Concurrency and idempotency

Two people approving, a batch changing during approval, two workers, a duplicate callback, a callback racing a requery, a cancellation racing a debit, a
payment through another channel, a retry racing the original response: each is handled by row locks, atomic state changes, unique constraints
(one debit item per family per batch, one provider request reference per connection, one transaction per request, one payment per debit) and the
deterministic 13-digit reference every debit and mandate carries.

## API

`/api/v1/schools/<school>/mandates/`: `overview/`, `providers/`, `connections/` (+ `<id>/test|rename|disable|enable|replace-credentials|disconnect|webhook-token`,
`webhook/`, `banks/`, `audit/`), `families/<id>/payers/`, `mandates/` (+ `<id>/`, `refresh|retry-setup|resend-activation|cancel|suspend|reactivate|primary`),
`my-mandates/` for the payer (+ `<id>/`, `consent|activation-request|activation-confirm|refresh|cancel`), `debit-batches/` (+ `<id>/`, `items/`,
`events/`, `progress/`, `failed/`, `retry/`, `preview|selection|title|submit|approve|reject|cancel|start`, `items/<id>/amount/`), `transactions/`
(+ `<id>/check/`); and the public callback `/api/v1/mandate-webhooks/<provider>/<token>/`. Refusals carry a stable `code`; `409` means "what you were
looking at is out of date". Everything is scoped to the acting membership's school (another school's data answers 404).

## Running it

`python manage.py run_mandate_jobs` (once, or `--loop` as a worker). It also asks each provider about every mandate it still has something to say
about. Settings: `MANDATES_ENABLE_SANDBOX`, `MANDATES_LENDSQR_LIVE_ENABLED` (default off), `MANDATES_LENDSQR_ACTIVATION_WINDOW_HOURS`,
`MANDATES_LENDSQR_DEBIT_SETUP_MINUTES`, `MANDATES_REMITA_LIVE_BASE_URL`, `MANDATES_REMITA_LIVE_FORM_BASE_URL`, `MANDATES_INLINE_JOBS`,
`MANDATES_INLINE_SECONDS`, and `BANKCONNECT_SECRET_KEYS` (required: with no key nothing is stored).

## Version 1 and what is deliberately not here

Version 1 is **manual and controlled**: no unattended recurring or scheduled debits, no automatic retry on insufficient funds, no debit whenever new
fees are raised, no automatic debit of arrears or after a term starts. The data model (mandate dates, per-mandate limits, the job queue, the
fingerprints) is built so a `MandateDebitSchedule` with instalment dates, pre-debit notices, balance-aware amounts and retry windows can be added
later without a rewrite, after Version 1 is proven.

Notifications are sent for authorisation needed, activation needed, activated, a problem, cancelled, a debit succeeded or failed, and a batch waiting
for approval or rejected. **No notice period is assumed**: how long before a debit a payer must be told is a legal and provider matter SchoolOS has not
verified, and the consent wording (versioned) should be reviewed by the school's advisers.

## Pending (nothing invented)

* **Lendsqr debits**: no documented way to send or query a debit on a mandate; refused as `pending_documentation` until Lendsqr documents it.
* **Lendsqr callbacks**: none documented; status is asked for (`run_mandate_jobs`, and the app's refresh).
* **Lendsqr live**: needs the provider/compliance position confirmed and `MANDATES_LENDSQR_LIVE_ENABLED`; the mandate amount unit is assumed to be naira.
* **Remita live**: needs its live host configured and a UAT with Remita; the API token and service type are only proven at the first activation and setup.
* **Remita** has no documented pause or reversal code: a mandate can be cancelled, and a reversal is recorded only if a documented status says so.
* **Fixed-amount mandates and scheduled debits** (Version 2).
* **A payer without an account in the app** authorises with the provider; there is no parent-facing form for one.
