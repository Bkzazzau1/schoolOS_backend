# Canonical Student Roster, Admissions and Enrollment

SchoolOS now has one server-authoritative student domain. The native application remains offline-first, but accepted sync mutations are materialized into canonical relational records inside the same transaction.

## Authority model

An admissions applicant is **not** a student.

The progression is:

```text
AdmissionApplication
    -> accepted offer
    -> StudentRegistration
    -> server accepts Active registration
    -> Student + GuardianLink + active StudentEnrollment
```

Only an active, billable `StudentEnrollment` contributes to the SchoolOS SaaS student meter.

The native app may show an offline registration as locally completed while it is queued. That does not mean the server has activated or billed the student. Canonical activation happens only after the sync mutation is accepted.

## Canonical records

- `AdmissionApplication` — admissions pipeline only; never billable.
- `StudentRegistration` — registration workflow and immutable permanent identifiers.
- `Student` — canonical school-scoped identity. Its UUID is the safe opaque QR/barcode identity.
- `GuardianLink` — guardian/family relationship kept separate from the student identity.
- `StudentEnrollment` — append-only class/enrollment history. Exactly one active enrollment is allowed per student.
- `StudentLifecycleEvent` — append-preserved operational history for class changes, promotions, transfers, withdrawals and graduation.
- `SchoolRosterRevision` — monotonic revision of billable roster membership.

Admission numbers and student codes are unique within a school and are never recycled by the canonical workflow.

## Offline sync contract

The existing sync outbox remains the write path for the native administrator workspace.

Canonical handlers own these entity types:

- `admission_applicant`
- `student_registration`
- `administrator_student_lifecycle`

Handlers validate role authority and legal state transitions before the generic `SyncRecord` is accepted. Their `after_write` hooks materialize the canonical models in the same database transaction. A canonical validation failure therefore rolls back the sync write as well.

Important invariants:

- Admissions stages cannot move backward.
- A closed application is not reopened by editing the record.
- `Registered` requires a successfully activated canonical registration.
- Active registration cannot return to `Admission in progress`.
- Permanent identifiers cannot be rewritten.
- Active class placement changes only through lifecycle workflows.
- Lifecycle events start Pending and Completed/Cancelled states are terminal.
- Promotion completion requires the academic approver.
- Transfer completion requires the records pack.

## Enrollment and billing rules

Billable count is:

```text
count(StudentEnrollment where status = active and is_billable = true)
```

This means:

- Applicant: not billable.
- Registration draft: not billable.
- Server-accepted Active registration: billable.
- Class change: still billable; old enrollment closes and a new active enrollment is appended.
- Promotion: still billable; old enrollment closes and a new active enrollment is appended.
- Transfer pending: still billable until transfer completes.
- Transfer completed: no longer billable.
- Withdrawal completed: no longer billable.
- Graduation / Alumni transition: no longer billable.

Every billable membership change publishes an immutable billing meter with source `canonical_student_roster` and a new roster revision.

A newly provisioned school publishes an authoritative zero-student meter immediately. Zero is real usage; absence of a meter is not interpreted as zero.

## Fresh period observations

Automatic billing requires a meter measured inside each billing period. Even when the roster does not change, publish a fresh authoritative observation regularly:

```bash
python manage.py publish_roster_meters
```

Run this daily before the automatic billing cycle command:

```bash
python manage.py run_billing_cycles
```

Running `publish_roster_meters` repeatedly on the same day is idempotent for an unchanged roster.

## Alumni integration

The existing Alumni Management workflow remains responsible for Alumni identities. A `TRANSITIONED` Alumni verification event is mirrored into the canonical roster when its admission number or former-student reference resolves to a canonical student.

The mirror runs inside the same outer transaction:

1. active enrollment closes as Graduated;
2. student status becomes Graduated;
3. canonical lifecycle history is appended;
4. the billable roster meter is republished.

Legacy Alumni transitions without a canonical student match remain supported and are not blocked.

## Read API

Authenticated school-scoped reads are available at:

```text
GET /api/v1/schools/{schoolId}/students/
GET /api/v1/schools/{schoolId}/students/{studentUuid}/
GET /api/v1/schools/{schoolId}/admissions/
GET /api/v1/schools/{schoolId}/roster/
```

Mutation authority remains the offline sync workflow for the native administrator app. The client never supplies a billing student count.

## QR and privacy boundary

Use the canonical `Student.id` UUID as the opaque machine identity when a QR/barcode is required. Do not encode date of birth, guardian phone, health information, fee balance or other sensitive data directly into a QR/barcode.
