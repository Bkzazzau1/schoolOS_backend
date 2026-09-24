# Subjects, Class Curriculum and Teaching Assignments

This document defines the canonical SchoolOS academic-subject contract introduced after Academic Sessions, Terms, Classes and progression became authoritative.

## Authority chain

SchoolOS now treats the academic chain as:

`Academic Session → Academic Class → Class Subject → Term Topics → Teaching Assignment → Student Subject Eligibility`

The app may work offline, but local queued records are not canonical until the server accepts them.

## Subject catalog

`Subject` is the school-wide canonical catalog. A subject has a unique school code and name, an optional section scope and an active/inactive state.

A section-scoped subject may only be placed in a matching class. A blank section means the subject may be used across sections.

Once curriculum history exists, the subject identity cannot be silently rewritten. An active subject cannot be deactivated while it is still used by active class curriculum.

## Class curriculum

`ClassSubject` binds one Subject to one AcademicClass for one AcademicSession.

It owns:

- compulsory vs elective requirement;
- periods per week;
- active/inactive state.

Weekly periods are curriculum authority. A teacher assignment does not own or override periods per week.

Closed academic-session curriculum is historical and cannot be changed.

## Term topics

`CurriculumTopic` is an ordered topic under one ClassSubject and one AcademicTerm.

Topics must belong to a term in the same session as the ClassSubject. Once a term closes, its topic sequence/title/description are historical and cannot be rewritten.

Teacher private workspace links contain only the topics for the currently active term.

## Student subject eligibility

Compulsory ClassSubjects are automatically eligible for every pupil whose active `EnrollmentAcademicContext` points to that session and class.

Elective ClassSubjects require an explicit `StudentSubjectSelection` for that pupil's current enrollment context.

The selection belongs to the immutable enrollment context, not to the student's permanent identity. Therefore promotion or a new academic-session enrollment does not silently carry old electives into the new class.

The private Student and Parent workspace payloads publish:

- `eligibleSubjects`: compulsory subjects plus explicitly selected electives;
- `availableElectives`: electives offered to the class but not selected.

Curriculum or elective changes republish the affected Student and Parent private links.

## Teaching assignments

`TeachingAssignment` is an effective-dated responsibility for teaching one canonical ClassSubject.

It points to a real active `Membership(role=teacher)`. A staff record, free-text person name or provisional identity is not sufficient authority to receive class access.

Only one active TeachingAssignment may exist for a ClassSubject at a time.

The Principal Teaching Assignments surface remains the native operational UI, but the server canonicalizes every accepted assignment against:

- the active academic session;
- the canonical AcademicClass;
- the canonical ClassSubject;
- a real active Teacher membership.

A Principal is restricted to Secondary curriculum/assignment scope.

### Handover

Changing the teacher preserves the previous responsibility as an ended historical TeachingAssignment linked through `previous_assignment`. The active external assignment id remains stable for the current responsibility.

The receiving teacher gets a new private `teacher_class_assignment` payload; the previous teacher's private link is republished without the handed-over class.

Provisional teaching targets are not canonical. Staff must be onboarded and have an activated Teacher membership first.

## Teacher private workspace

The server-generated `teacher_class_assignment` record is private to exactly one Teacher membership. It contains each active class-subject assignment with canonical ids, curriculum periods, current term and current-term topics.

Real backend-connected schools do not seed teacher demo assignments. Standalone demo mode remains separate.

## Syllabus progress

Teacher syllabus rows in backend mode are built from canonical current-term CurriculumTopics.

The topic UUID is the progress-record identity. This prevents collisions when one teacher handles multiple subjects in the same class.

Teachers may report only:

- `completed`;
- `inProgress`.

The server accepts progress only when:

- the topic exists in the teacher's school;
- the term is not closed;
- the signed-in Teacher currently holds the TeachingAssignment for that ClassSubject.

The server owns the actor, canonical class, topic sequence, version increment and timestamp. Principal/Administrator/Proprietor can read validated progress records. Principal Academics calculates syllabus coverage from canonical active-term topic ids and these validated teacher reports, not from demo scheme rows.

## Term changes

When AcademicTerm state changes, SchoolOS republishes:

- active Student private profile links;
- linked Parent family payloads;
- affected Teacher assignment/topic payloads.

This prevents a device from continuing to show an old current term after the school activates the next term.

## Existing-school rollout

After migration/configuration, deployment may run:

`python manage.py bootstrap_teaching_assignments`

This attempts to materialize legacy `principal_teaching_assignment` sync records. It can resolve an old staff-record id through `owner_staff_profile.linkedMembershipId`. Unresolved curriculum or Teacher identities are skipped rather than guessed.

Then run:

`python manage.py refresh_curriculum_workspace_links`

This is idempotent and republishes current subject eligibility to Student/Parent workspaces and active assignments/topics to Teacher workspaces.

`refresh_teacher_assignment_links` remains available as the narrower Teacher-only refresh command.

## Offline semantics

Subject/catalog/curriculum edits and Principal teaching assignment changes can be queued locally, but queued does not mean accepted.

Teacher syllabus progress can be recorded offline and queued, but the approved curriculum itself cannot be rewritten by Teacher progress.

A server rejection must remain visible through normal SchoolOS sync handling; the client must not infer authority from a local queued record.
