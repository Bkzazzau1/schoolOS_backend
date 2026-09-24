# Subjects, Class Curriculum and Teaching Assignments

This document defines the canonical SchoolOS academic-subject contract introduced after Academic Sessions, Terms, Classes and progression became authoritative.

## Authority chain

SchoolOS treats the academic chain as:

`Academic Session → Academic Class → ClassSubject → TeachingAssignment → Student Subject Eligibility`

`CurriculumTopic` adds the term-specific scheme under a ClassSubject.

The app may work offline, but a local dirty/queued record is not canonical until the server accepts it. Client screens must never infer authority from class names, hardcoded subjects, free-text teacher names or demo assignment rows.

## Subject catalog

`Subject` is the school-wide canonical catalog. A subject has a unique school code and name, an optional section scope and an active/inactive state.

A section-scoped subject may only be placed in a matching class. A blank section means the subject may be used across sections.

Once curriculum history exists, subject identity cannot be silently rewritten. An active subject cannot be deactivated while it is still used by active class curriculum.

## Class curriculum

`ClassSubject` binds exactly one Subject to one AcademicClass for one AcademicSession.

It owns:

- compulsory vs elective requirement;
- periods per week;
- active/inactive state.

Periods per week belong to curriculum authority. TeachingAssignment does not independently own or override the weekly requirement.

Closed academic-session curriculum is historical and cannot be rewritten. A later session receives its own ClassSubject records, so changing JSS 2A Mathematics from five periods to six in a future session does not alter the earlier session.

## Term topics

`CurriculumTopic` is an ordered topic under one ClassSubject and one AcademicTerm.

The term must belong to the same AcademicSession as the ClassSubject. Once a term closes, its topic sequence/title/description are historical and cannot be rewritten.

Teacher private assignment links contain only topics for the currently active term.

## Student subject eligibility

The current pupil curriculum is derived from the pupil's active `EnrollmentAcademicContext`.

Compulsory ClassSubjects are automatically eligible for every pupil whose active enrollment context points to that session and class. No StudentSubjectSelection row is created merely to represent a compulsory subject.

Elective ClassSubjects require an explicit `StudentSubjectSelection` for that exact enrollment context.

The elective selection belongs to the enrollment/session/class context, not permanently to the Student. Promotion, repetition into a new session or another new enrollment therefore does not silently carry an old elective forward.

The private Student and Parent workspace payloads publish:

- `eligibleSubjects`: compulsory subjects plus explicitly selected electives;
- `availableElectives`: class electives that are not selected.

The Student `My Subjects` surface must display available-but-unselected electives separately from subjects the pupil is actually taking. Parent child profiles expose only the selected child's own eligible subjects.

Curriculum and elective changes republish the affected Student/Parent private links.

## Progression and lifecycle behavior

Promotion creates a new active enrollment context for the destination AcademicSession and AcademicClass. Subject eligibility is recalculated from the destination ClassSubject curriculum. Old curriculum and elective choices remain historical.

When an enrollment stops being active because of promotion, repeat, class change, transfer out, graduation or withdrawal, Student/Parent private links are republished. Promotion/repeat/class-change then publish again when the replacement active enrollment receives its academic context. Terminal lifecycle changes have no replacement context, so current subject eligibility becomes empty.

## Teaching assignments

`TeachingAssignment` is an effective-dated responsibility for teaching one canonical ClassSubject.

It points to a real active `Membership(role=teacher)`. A staff record, free-text person name or provisional staff identity is not sufficient authority to receive class access.

A staff profile may decorate the teacher with HR information, but its `linkedMembershipId` must resolve to the active Teacher membership used as `teacherId`.

Only one active TeachingAssignment may exist for a ClassSubject at a time.

The Principal Teaching Assignments surface may assign only subjects that already exist in the selected class curriculum. Coverage is therefore:

`active ClassSubject requirements - active TeachingAssignments`

A Principal is restricted to Secondary academic scope by the server.

### Handover

Changing the teacher preserves the previous responsibility as an ended historical TeachingAssignment linked through `previous_assignment`. The active external assignment id remains stable for the current responsibility.

Both the old and new Teacher private links are refreshed. The previous teacher loses the handed-over class; the receiving teacher gains it.

Provisional teaching targets may exist during onboarding, but they are not canonical TeachingAssignments until the staff profile is linked to an active Teacher membership.

## Teacher private workspace contract

The server owns the private sync entity `teacher_class_assignment`, keyed to exactly one Teacher membership. It contains only that membership's current assignments.

Each assignment publishes:

- class name;
- subject name;
- `sessionId`;
- `classId`;
- `subjectId`;
- `classSubjectId`;
- `teachingAssignmentId`;
- curriculum `periodsPerWeek`;
- `currentTermId` and current term name;
- current-term curriculum topics.

The native `My Classes` screen displays this canonical metadata and derives its roster from pupils currently enrolled in the assigned class. It does not use demo assignment seeds in backend-connected mode.

## Term changes

When AcademicTerm state changes, SchoolOS republishes:

- active Student private profile links;
- linked Parent family payloads;
- active Teacher assignment/topic payloads for the session.

This prevents devices from continuing to show an old current term after the school activates another term.

## Existing-school curriculum bootstrap

SchoolOS does not invent a Nigerian, national or other default curriculum. Existing schools can provide an explicit JSON manifest and run:

`python manage.py bootstrap_curriculum --school <school-slug-or-uuid> --session <session-code-or-uuid> --file curriculum.json`

Optional attribution can be supplied with:

`--actor-membership <membership-uuid>`

The actor must be an active Administrator, Proprietor or Principal. Principal scope rules still apply.

Example manifest:

```json
{
  "subjects": [
    {
      "code": "MATH",
      "name": "Mathematics",
      "shortName": "Maths",
      "section": "Secondary",
      "isActive": true
    }
  ],
  "curriculum": [
    {
      "classCode": "JSS2A",
      "subjectCode": "MATH",
      "requirement": "compulsory",
      "periodsPerWeek": 5,
      "isActive": true
    }
  ]
}
```

`requirement` and `periodsPerWeek` are explicit. The command does not guess whether a subject is compulsory/elective or how many weekly periods it receives. It is safe to rerun against the same canonical identities.

## Legacy Teaching Assignment backfill

After curriculum exists, deployment may run:

`python manage.py bootstrap_teaching_assignments`

The command inspects legacy `principal_teaching_assignment` sync records and materializes a canonical TeachingAssignment only when it can resolve:

- one canonical ClassSubject;
- one active Teacher membership, either directly or through `owner_staff_profile.linkedMembershipId`;
- an active Principal/Proprietor actor for attribution.

Existing canonical assignments are confirmed and their sync payloads are normalized. Ambiguous class/subject matches, unresolved Teacher identities and invalid assignment ids are reported and skipped rather than guessed. The command is idempotent and does not destructively rewrite historical handovers.

Then run:

`python manage.py refresh_curriculum_workspace_links`

This idempotently republishes current subject eligibility to Student/Parent workspaces and active assignments/topics to Teacher workspaces.

`refresh_teacher_assignment_links` remains available as the narrower Teacher-only refresh command.

## Syllabus progress

Teacher syllabus rows in backend mode are built from canonical current-term CurriculumTopics.

The topic UUID is the progress-record identity. Teachers may report only progress against a topic that exists in their school, belongs to an open term, and belongs to a ClassSubject they currently teach. Teacher progress never rewrites the approved curriculum itself.

## Tenant and role boundaries

Every Subject belongs to exactly one School tenant. ClassSubject, CurriculumTopic, StudentSubjectSelection and TeachingAssignment resolution is validated through that tenant chain before acceptance.

Teaching authority is the Teacher Membership. Student/Parent/Teacher private links are visible only to the exact memberships for which the server publishes them.

Principal curriculum/assignment writes are restricted to Secondary scope. Administrator/Proprietor operations remain school-tenant scoped.

## Offline semantics

Subject/catalog/curriculum edits and Principal teaching-assignment changes may be queued locally, but queued does not mean accepted, assigned, published or canonical.

The server validates session state, school ownership, class/subject identity, Teacher membership authority, elective context and historical immutability before canonical acceptance.

A server rejection remains visible through normal SchoolOS sync handling; the client must never infer authority from a locally queued record.
