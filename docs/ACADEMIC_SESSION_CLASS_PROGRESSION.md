# Academic Session, Term, Class Structure and Bulk Progression

## Canonical authority

SchoolOS treats the server academic domain as the authority for school-year context. A class name on a device is not enough to determine academic progression.

The canonical chain is:

`AcademicSession -> AcademicTerm -> AcademicClass -> EnrollmentAcademicContext -> StudentEnrollment`

`StudentEnrollment` remains append-only. Promotion and repeat close the previous enrollment row and create a new active row. Historical placement is never rewritten.

## Academic sessions

A school may have many historical/planned sessions but only one active session.

Session states are:

- `planned`
- `active`
- `closed`

A closed session cannot be reopened. An active term must be closed before its session can be closed.

## Academic terms

Terms belong to exactly one session and have an explicit sequence and date range. A term must fall inside its parent session. Only one term in a session may be active at a time, and a term can be activated only when its session is active.

`EnrollmentAcademicContext.entry_term` records the term in which that enrollment began. The private Student/Parent workspace also receives the session's current active term separately, so later term changes do not rewrite the pupil's entry history.

## Class structure

`AcademicClass.level_order` establishes progression order. `next_class` defines the school's normal promotion route. `is_terminal` marks a class from which graduation may occur.

Once a class has enrollment history, its structural identity (`code`, `name`, `section`, `level_order`, `stream`) cannot be silently rewritten. A class with active pupils cannot be deactivated.

Repeat is not represented by setting `next_class` to itself. Repeat is an explicit academic progression outcome.

## Existing schools

Academic configuration is additive and backward-compatible. Existing active student enrollments continue to work even before academic sessions/classes are configured.

After configuring the active session, active term and canonical class names, run:

```text
python manage.py bootstrap_academic_placements
```

The command is idempotent. It attaches academic context to matching active enrollments and republishes private Student/Parent workspace links. Unmatched enrollments are skipped rather than guessed.

## Bulk progression lifecycle

A progression batch belongs to one source session, one destination session and one source class. Every active pupil in that source session/class must have exactly one explicit decision.

Supported decisions:

- `promote`: move to a higher active canonical class (normally `next_class`)
- `repeat`: remain in the same class in the destination session
- `transfer_out`: leave the school; records pack must be ready
- `graduate`: allowed only from a terminal class
- `hold`: unresolved decision; blocks application

The safe end-of-session sequence is:

1. Prepare/review progression decisions while the source session is active.
2. Resolve every `hold`.
3. Close the active term.
4. Close the source session.
5. Record the authorized academic approver.
6. Apply the progression batch.
7. Activate the destination session/term when the school's calendar requires it.

The backend refuses to apply a batch while the source session is still active/planned. The destination session must be later than the source and must not be closed.

## Server confirmation and offline operation

The native app is offline-first. Saving a review or pressing **Apply progression** creates a durable sync mutation, but **queued does not mean applied**.

Only a canonical batch returned by the server with:

- `status = applied`
- a non-null `appliedAt`
- no local dirty/pending state

is presented as successfully applied.

If the server rejects a batch, no student is partially progressed because batch application is transactional.

## Reuse of student lifecycle authority

Bulk progression does not implement a second student-movement engine. It creates the same canonical lifecycle events used by individual student administration and delegates to the existing student lifecycle service.

Therefore:

- promotion and repeat preserve enrollment history;
- stale/source-class checks remain authoritative;
- transfer and graduation use the established exit rules;
- Student and Parent private workspace links are republished after each confirmed change.

## Billing

Promotion and repeat replace one billable active enrollment with one billable active enrollment and therefore do not change the number of billable students.

Transfer out and graduation end billable enrollment and continue to publish canonical roster-meter changes through the existing student lifecycle service.

## Academic decision boundary

Administration maintains the academic calendar/class structure and processes a progression batch. Promotion, repeat and graduation outcomes require an identified academic approver. The Administrator workspace does not turn operational processing into independent academic decision authority.
