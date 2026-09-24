# SchoolOS Automatic Billing Cycle

## Purpose

Automatic SaaS billing belongs to the Organization account, not to a school's finance ledger. The engine converts trusted school roster counts into an immutable organization usage snapshot, then into one immutable invoice for the billing period.

The engine never derives billable students from user accounts or `schools.Membership`. A student may exist without a SchoolOS login, so login membership is not a reliable commercial meter.

## Authoritative roster meter

`SchoolBillingMeterSnapshot` stores only a count, not student records. It is append-only and records:

- school
- billable student count
- source
- source version
- whether the source is authoritative
- effective measurement time
- optional metadata

The canonical Student module should call:

```python
publish_school_billing_meter(
    school=school,
    billable_student_count=count,
    source="students.roster",
    source_version=roster_version,
    measured_at=measured_at,
    authoritative=True,
)
```

`source_version` makes retries idempotent. Until the canonical Student module owns this hook, an operator/integration can bootstrap the same contract with:

```bash
python manage.py publish_billing_meter \
  --school <school-uuid> \
  --count 850 \
  --source trusted_roster_import \
  --version 2026-09-24T06:00Z
```

This is a transitional server-side integration hook, not a client-entered invoice amount.

## Automatic billing policy

Every plan can have one `BillingCyclePolicy` with:

- `automatic_invoicing_enabled`
- `invoice_due_days`
- `past_due_days`
- `grace_days`

The timing values are nullable intentionally. SchoolOS does not invent commercial terms. Automatic billing remains not ready until all values are explicitly configured and the plan has a billing interval.

## Period behaviour

### Monthly / annual

When automatic billing is enabled and no period exists, the cycle runner initializes a period from the current server time. At the period end it:

1. verifies there is no older open invoice;
2. requires an authoritative roster meter for every active school;
3. captures an immutable organization `UsageSnapshot`;
4. calculates the invoice from the server-side plan;
5. sets the due date from `invoice_due_days`;
6. advances to the next monthly/annual period.

### Term / custom

The period dates must be explicitly set on the organization subscription. After the period is invoiced, the dates are cleared and SchoolOS waits for the next explicit term/custom period. This prevents SchoolOS from guessing school calendars.

## Non-payment lifecycle

For an open invoice:

- after `due_at`: `Past Due`
- after `past_due_days`: `Grace`
- after an additional `grace_days`: `Restricted`

`Past Due` and `Grace` retain the current full school access policy. `Restricted` prevents account-expansion actions while existing school data remains preserved. A verified successful payment uses the existing payment settlement path to restore the subscription to `Active`.

## Scheduler

Run the command from cron, a container scheduler, Windows Task Scheduler, or a future Celery beat process:

```bash
python manage.py run_billing_cycles
```

The command is safe to repeat. Each organization subscription is row-locked while processed, one open invoice blocks stacking another invoice, and the invoice/usage/period changes are committed atomically.

A daily run is sufficient for day-based due/grace policies. Running hourly is also safe.

## Activation checklist

Automatic charging is intentionally dormant until all of the following are true:

1. the plan has a billing interval;
2. a `BillingCyclePolicy` exists and automatic invoicing is enabled;
3. due, past-due and grace durations are configured;
4. monthly/annual can initialize its period, or term/custom has explicit period dates;
5. every active school has a trusted authoritative roster meter for the period;
6. Paystack production configuration is present before payment collection.

This means deploying the code cannot unexpectedly start charging existing schools.
