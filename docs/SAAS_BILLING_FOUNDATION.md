# SchoolOS SaaS billing foundation

This document defines the commercial account layer above SchoolOS school tenants.

## Boundary

School finance and SchoolOS SaaS billing are different domains.

```text
SchoolOS platform
  -> Organization (commercial customer account)
       -> OrganizationSubscription
            -> Plan
            -> Entitlements
            -> Usage snapshots
       -> School A (operational tenant)
       -> School B (operational tenant)
```

School fees, payroll, concessions and other operational money remain inside each
school tenant. Subscription billing belongs to the organization account and must
never be used as a substitute for school-role authorization.

## Baseline plan

The initial catalog contains `standard` / `SchoolOS Standard`.

- Currency: NGN
- Base amount: 0
- Student unit amount: 50,000 minor units (NGN 500)
- Billing interval: intentionally unset
- `school_provisioning`: enabled
- `multi_school`: enabled

The per-student amount is represented now, but no monthly/term/annual cadence is
assumed by this foundation. A later commercial decision can set the interval
without changing the entitlement or subscription schema.

Existing active organizations are migrated onto the baseline plan with an Active
subscription. New organizations receive the same subscription atomically when
the organization is created.

## Subscription lifecycle

Supported states:

- `trial`
- `active`
- `past_due`
- `grace`
- `restricted`
- `suspended`
- `cancelled`

The current access-mode mapping is deliberately conservative:

- trial / active / past_due / grace -> `full`
- restricted -> `account_restricted`
- suspended / cancelled -> `suspended`

`account_restricted` blocks new account-level expansion such as provisioning a
school, but it does not delete or rewrite existing school data. Operational
school restrictions, offline entitlement leases and payment-recovery rules are
future layers and must be implemented explicitly rather than inferred in Flutter.

## Entitlements

Plan capabilities are normalized in `PlanEntitlement`.

Each entitlement has:

- `code`
- `enabled`
- optional integer `limit_value`
- metadata JSON for future capability-specific configuration

The backend is authoritative. Native/web clients may display entitlement state,
but must not decide commercial authorization locally.

School provisioning currently checks:

1. organization account role
2. email-verification expansion rule
3. subscription access mode
4. `school_provisioning` entitlement and optional limit
5. `multi_school` entitlement when a second school is being created

## Usage snapshots

`UsageSnapshot` is append-only billing-meter evidence.

It contains active school count and an optional `billable_student_count`.
The student count is intentionally **not** calculated from Student login
memberships because a school may have enrolled students without SchoolOS login
accounts. A future meter must capture the authoritative enrolled/billable student
population from school data and write a snapshot for the billing period.

## Audit history

`SubscriptionEvent` is append-only lifecycle history. Events are created for:

- subscription bootstrap
- migration of existing organizations
- future service-driven state transitions
- manual Django-admin status changes
- manual Django-admin plan changes

Payment-provider webhooks must use the transition service and add provider event
identifiers to the event detail so webhook processing can later be made
idempotent.

## API

### GET `/api/v1/plans/`

Returns the active public plan catalog for a signed-in account.

### GET `/api/v1/organizations/{organizationId}/subscription/`

Returns the current commercial state for an organization the signed-in person
belongs to.

Shape:

```json
{
  "organizationId": "...",
  "status": "active",
  "accessMode": "full",
  "plan": {
    "code": "standard",
    "name": "SchoolOS Standard",
    "currency": "NGN",
    "billingInterval": null,
    "baseAmountMinor": 0,
    "studentUnitAmountMinor": 50000
  },
  "canManageBilling": true,
  "entitlements": {
    "school_provisioning": {
      "enabled": true,
      "available": true,
      "limit": null,
      "metadata": {}
    }
  },
  "usage": {
    "activeSchools": 1,
    "billableStudents": null,
    "capturedAt": null
  }
}
```

Provider customer IDs and provider subscription IDs are intentionally not exposed
to clients.

## Next commercial phase

The next phase can add a payment adapter without redesigning the account layer:

1. choose billing cadence and trial/grace policy
2. choose provider(s)
3. create checkout/payment-session commands server-side
4. verify signed provider webhooks
5. make webhook processing idempotent
6. create invoices/payment records
7. capture authoritative student usage snapshots
8. transition subscription states through the audited service
9. add an offline entitlement lease for operational restrictions, if required

No provider SDK or secret should be embedded in the Flutter application.
