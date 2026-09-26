# Smart Money Collection (backend)

How a school collects fees from families through **its own** Paystack, Monnify or Remita account, and how SchoolOS finds out what
was paid. This file says what is real, what is pending and how to run it. School fees are the **school's** money: SchoolOS never
receives, holds or settles it. (What schools pay SchoolOS - the SaaS subscription - is `apps/billing`, a different thing entirely.)

```
School's own provider account -> school's own credentials -> SchoolOS connector -> ONE active provider
   -> a family's collection account -> the provider moves the money -> signed provider event
   -> SchoolOS reconciliation -> the family receivables ledger (canonical)
```

## What lives where

| App | Owns |
|---|---|
| `apps/bankconnect` | the provider connections (credentials, webhook, environment), the connector interface and the three adapters, provider events, matching and the review queue |
| `apps/receivables` | families, charges, adjustments, family credit, the family collection accounts and their statements - the canonical ledger |
| `apps/smartcollect` | policy, batches, approval, generation, provider switching (see the second half of this file) |

## Provider connections (`apps/bankconnect`)

`CollectionProviderConnection` (was `BankConnection`; the old name is an alias, the table was renamed in place by migration
`bankconnect 0004`, so no payment or reconciliation history moved or was lost). It holds only safe facts: provider, environment,
merchant name and masked reference, status, webhook status and when it was last verified, plus the **sealed** credential.

* The school enters credentials **its provider issued to it**: Paystack `secret_key`; Monnify `api_key`, `secret_key`,
  `contract_code`; Remita `merchant_id`, `api_key`, `service_type_id`. There is no settlement account number, no account
  fingerprint and no "is this your school's account?" question.
* Credentials are sealed by the vault (`vault.py`, MultiFernet, `BANKCONNECT_SECRET_KEYS`, bound to school and connection so a
  copied ciphertext does not open). No serializer, log line, error message, audit detail or admin field can carry one; a provider's
  own error text is never copied, only SchoolOS's words.
* A school may connect several providers (one live connection per provider per school, enforced by the database) but has exactly
  **one active collection provider** (also a database rule). It is chosen once with `activate`; any later change is a reviewed
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

| | Paystack | Monnify | Remita |
|---|---|---|---|
| Family collection account | Dedicated Virtual Account for a Customer | Customer Reserved Account | an invoice: an **RRR** payment reference (Remita has no virtual bank accounts) |
| Static / dynamic | static, dynamic | static, dynamic | **dynamic only** (an RRR is for one amount) |
| Deactivate / reactivate / close | deactivate and close (`DELETE`); no reactivate documented | close only | close (cancel the RRR) |
| Customer KYC | no | **BVN or NIN required** | no |
| Webhook authenticity | `x-paystack-signature`, HMAC-SHA512 of the body with the secret key | `monnify-signature`, HMAC-SHA512 with the client secret - live only; sandbox is unsigned so every event is confirmed by requery | **no signature documented**: every RRR is confirmed with Remita's status API, and its answer (not the body) decides |
| Webhook set up | in the dashboard | in the dashboard | in the dashboard; Remita is answered `Ok` |
| Direct-debit mandates | - | capability only | capability only (never part of the family-accounts model) |

Pending for lack of official documentation or credentials (nothing is invented): Remita's **live** host is not in its published
documentation, so a live Remita connection is refused until an operator sets `COLLECTION_REMITA_LIVE_BASE_URL`; Monnify's contract
code is only proven when the first account is made; a Remita service type is only proven when the first invoice is made. Neither is
claimed as verified before then.

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

## Tests

`apps.bankconnect` (connector, connection API, webhook and reconciliation tests) and `apps.receivables`. A connector is tested
against its provider's documented shapes; no test calls a real provider.
