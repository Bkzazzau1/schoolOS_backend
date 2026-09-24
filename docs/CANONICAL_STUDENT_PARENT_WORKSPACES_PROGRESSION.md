# Canonical Student/Parent Workspaces and Class Progression

## Authority

`students.Student` and append-only `students.StudentEnrollment` rows are the source of truth for pupil identity and class placement. Native Student and Parent workspaces consume private server-generated sync records; they do not infer the pupil's current class locally.

The current class is the class on the single active enrollment. Historical enrollments are never rewritten to make a pupil appear to have always been in the latest class.

## Progression workflows

SchoolOS distinguishes these outcomes:

- **Promotion** — academic decision; requires an approver and a different destination class. The previous enrollment closes and a new active enrollment opens in the destination class.
- **Repeat** — academic decision; requires an approver and keeps the same class. The previous enrollment closes and a new active enrollment opens in that same class, preserving the fact that a new progression period began.
- **Class change** — operational movement to a different class; it is not recorded as promotion or repeat.
- **Transfer out / Withdrawal / Graduation** — closes the active enrollment and removes billability. The historical Student/Parent profile remains visible but has no active class.

A pending Promotion, Repeat or Class change records its source class. Completion is rejected if the pupil's current active class no longer matches that source class, preventing stale decisions from moving the wrong enrollment.

Class labels can also move the canonical academic section. Known Early Years, Primary and Secondary labels are mapped to their section; unrecognised/custom labels preserve the prior section rather than guessing.

## Billing

Promotion, Repeat and Class change replace one billable active enrollment with another inside the same transaction and do not publish a roster-count change. Transfer out, Withdrawal and Graduation close the billable enrollment and publish a canonical roster meter change.

## Private native workspace records

### `student_class_link`

Visible only to the matching Student membership. It contains the canonical Student profile, current enrollment state, enrollment history and progression history. When there is no active enrollment the record remains for historical access, but `className` is null and `enrollmentActive` is false so class-scoped CBT/resources are no longer available.

### `parent_family_link`

Visible only to the matching Parent membership. It contains only children linked to that Parent in the same school, with each child's canonical admission identity, current class/status, enrollment history and progression history.

These records are read-only to native clients and are republished from canonical state after Student/Enrollment changes.

## Deployment refresh

After deploying this feature, run:

`python manage.py refresh_student_workspace_links`

The command is idempotent and rebuilds the private Student and Parent workspace records for existing canonical pupils. It does not change enrollment or billing state.
