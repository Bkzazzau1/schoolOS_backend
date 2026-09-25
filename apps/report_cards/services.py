from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.academics.models import (
    AcademicClass,
    AcademicLifecycleStatus,
    AcademicTerm,
    ClassSubject,
    EnrollmentAcademicContext,
)
from apps.assessments.models import AssessmentDefinition, AssessmentScore, AssessmentState
from apps.assessments.services import grade_for_percent
from apps.core.errors import Rejected
from apps.lesson_attendance.services import student_term_attendance_percent
from apps.schools.models import Membership, Role
from apps.students.models import EnrollmentStatus, Student
from apps.sync.models import SyncRecord

from .models import ReportCard, ReportCardEvent, ReportCardState, ReportCardSubjectLine

REPORT_CARD_ENTITY = "academic_report_card"


def _membership_name(membership):
    if membership is None:
        return ""
    user = membership.user
    getter = getattr(user, "get_full_name", None)
    value = getter().strip() if callable(getter) else ""
    return value or getattr(user, "email", "") or str(user)


def _assert_manager_authority(membership):
    if membership.role not in {Role.ADMINISTRATOR, Role.PROPRIETOR} or not membership.is_active:
        raise Rejected("Only the Proprietor or Administrator can compile, submit or release report cards.")


def _assert_principal_secondary_authority(membership, academic_class):
    if membership.role != Role.PRINCIPAL or not membership.is_active:
        raise Rejected("Only an active Principal membership can record a report-card review.")
    if academic_class.section.strip().casefold() != "secondary":
        raise Rejected("Principal report-card review is limited to Secondary classes.")


def class_roster(academic_class: AcademicClass, term: AcademicTerm) -> list[Student]:
    contexts = EnrollmentAcademicContext.objects.filter(
        session_id=term.session_id,
        academic_class=academic_class,
        enrollment__status=EnrollmentStatus.ACTIVE,
    ).select_related("enrollment__student")
    students = [context.enrollment.student for context in contexts.distinct()]
    students.sort(key=lambda item: (item.full_name.casefold(), item.student_code.casefold()))
    return students


def _student_class(student: Student, term: AcademicTerm) -> AcademicClass | None:
    context = (
        EnrollmentAcademicContext.objects.filter(
            session_id=term.session_id,
            enrollment__student=student,
            enrollment__status=EnrollmentStatus.ACTIVE,
        )
        .select_related("academic_class")
        .first()
    )
    return context.academic_class if context else None


def _next_revision(report_card):
    latest = report_card.events.order_by("-revision").first()
    return (latest.revision if latest is not None else 0) + 1


def _append_event(report_card, *, actor, action, comment=""):
    ReportCardEvent.objects.create(
        report_card=report_card,
        revision=_next_revision(report_card),
        action=action,
        actor_membership=actor,
        comment=comment,
    )


def _loaded_report_card(school, external_id, *, lock=False):
    query = ReportCard.objects.select_related(
        "school", "student", "term__session", "academic_class"
    )
    if lock:
        query = query.select_for_update()
    item = query.filter(school=school, external_id=external_id).first()
    if item is None:
        raise Rejected("Report card does not exist in this school.")
    return item


def _subject_line(class_subject: ClassSubject, student: Student, term: AcademicTerm):
    """This student's weighted average and grade for one class subject in one
    term, computed only from RELEASED assessments - never a queued, submitted
    or locked one, matching the same release gate Student/Parent visibility
    already uses."""
    scores = (
        AssessmentScore.objects.filter(
            student=student,
            assessment__class_subject=class_subject,
            assessment__term=term,
            assessment__state=AssessmentState.RELEASED,
            score__isnull=False,
        )
        .select_related("assessment")
    )
    total_weight = Decimal("0")
    weighted_sum = Decimal("0")
    included = 0
    for score in scores:
        assessment = score.assessment
        if assessment.maximum_score <= 0:
            continue
        percent = score.score / Decimal(assessment.maximum_score) * 100
        weighted_sum += percent * assessment.weight
        total_weight += assessment.weight
        included += 1
    if total_weight <= 0:
        return None, "", 0
    weighted_percent = (weighted_sum / total_weight).quantize(Decimal("0.1"))
    return weighted_percent, grade_for_percent(weighted_percent), included


@transaction.atomic
def generate_report_card(*, school, student: Student, term: AcademicTerm, actor: Membership) -> ReportCard:
    if term.status == AcademicLifecycleStatus.CLOSED:
        raise Rejected("Report cards cannot be changed in a closed academic term.")
    academic_class = _student_class(student, term)
    if academic_class is None:
        raise Rejected(f"{student.full_name} has no active class enrollment for this term.")

    existing = (
        ReportCard.objects.select_for_update()
        .filter(school=school, student=student, term=term)
        .first()
    )
    if existing is not None and existing.state != ReportCardState.DRAFT:
        raise Rejected("A submitted, reviewed or released report card cannot be silently regenerated.")

    class_subjects = ClassSubject.objects.filter(
        session_id=term.session_id, academic_class=academic_class, is_active=True
    ).select_related("subject")

    lines = []
    percents = []
    for class_subject in class_subjects:
        weighted_percent, grade, included = _subject_line(class_subject, student, term)
        if weighted_percent is not None:
            percents.append(weighted_percent)
        lines.append((class_subject, weighted_percent, grade, included))

    overall_average = (sum(percents) / len(percents)).quantize(Decimal("0.1")) if percents else None
    overall_grade = grade_for_percent(overall_average)
    attendance_percent = student_term_attendance_percent(student, term)

    now = timezone.now()
    if existing is None:
        item = ReportCard.objects.create(
            school=school,
            external_id=f"rc-{student.id}-{term.id}",
            student=student,
            term=term,
            academic_class=academic_class,
            state=ReportCardState.DRAFT,
            overall_average=overall_average,
            overall_grade=overall_grade,
            attendance_percent=attendance_percent,
            generated_by=actor,
            generated_at=now,
            version=1,
        )
    else:
        item = existing
        item.academic_class = academic_class
        item.overall_average = overall_average
        item.overall_grade = overall_grade
        item.attendance_percent = attendance_percent
        item.generated_by = actor
        item.generated_at = now
        item.version += 1
        item.save()
        item.lines.all().delete()

    ReportCardSubjectLine.objects.bulk_create(
        [
            ReportCardSubjectLine(
                report_card=item,
                class_subject=class_subject,
                weighted_percent=weighted_percent,
                grade=grade,
                assessments_included=included,
            )
            for class_subject, weighted_percent, grade, included in lines
        ]
    )
    _append_event(item, actor=actor, action="generated")
    return item


@transaction.atomic
def compile_class_report_cards(
    *, school, academic_class: AcademicClass, term: AcademicTerm, actor: Membership
) -> list[ReportCard]:
    """Generate/regenerate every roster student's report card for this class
    and term, then rank them by overall average. Ranking is inherently
    class-wide, so it only ever happens here, never for one student alone.
    A student with no evidence yet is never assigned a fabricated position.
    """
    _assert_manager_authority(actor)
    roster = class_roster(academic_class, term)
    if not roster:
        raise Rejected("This class has no active students to compile report cards for.")

    cards = []
    skipped = []
    for student in roster:
        try:
            cards.append(generate_report_card(school=school, student=student, term=term, actor=actor))
        except Rejected as rejected:
            # A card already submitted/reviewed/released for this student is
            # left untouched rather than failing the whole class compile.
            skipped.append((student, rejected.message))

    ranked = sorted(
        (card for card in cards if card.overall_average is not None),
        key=lambda card: card.overall_average,
        reverse=True,
    )
    class_size = len(ranked)
    for position, card in enumerate(ranked, start=1):
        card.class_position = position
        card.class_size = class_size
        card.save(update_fields=["class_position", "class_size"])
    for card in cards:
        if card.overall_average is None:
            card.class_position = None
            card.class_size = class_size or None
            card.save(update_fields=["class_position", "class_size"])
    return cards


@transaction.atomic
def submit_report_card(*, actor: Membership, external_id: str) -> ReportCard:
    _assert_manager_authority(actor)
    item = _loaded_report_card(actor.school, external_id, lock=True)
    if item.state != ReportCardState.DRAFT:
        raise Rejected("Only a draft report card can be submitted.")
    item.state = ReportCardState.SUBMITTED
    item.submitted_by = actor
    item.submitted_at = timezone.now()
    item.version += 1
    item.save()
    _append_event(item, actor=actor, action="submitted")
    return item


@transaction.atomic
def class_teacher_comment_report_card(*, actor: Membership, external_id: str, comment: str) -> ReportCard:
    """The class/form teacher's own remark (apps.class_teachers). Available
    any time before release - it never affects a score or a state
    transition, so it is not restricted to one lifecycle step the way score
    entry or Principal review are."""
    from apps.class_teachers.services import current_class_teacher

    item = _loaded_report_card(actor.school, external_id, lock=True)
    if item.term.status == AcademicLifecycleStatus.CLOSED:
        raise Rejected("Report cards cannot be changed in a closed academic term.")
    if item.state == ReportCardState.RELEASED:
        raise Rejected("A released report card's class-teacher comment cannot be silently rewritten.")
    class_teacher = current_class_teacher(item.academic_class, item.term.session, required=False)
    if class_teacher is None or class_teacher.id != actor.id:
        raise Rejected("This membership is not the current class teacher for this student's class.")

    item.class_teacher_comment = comment.strip()
    item.version += 1
    item.save(update_fields=["class_teacher_comment", "version", "updated_at"])
    _append_event(item, actor=actor, action="class_teacher_commented", comment=comment.strip())
    return item


@transaction.atomic
def principal_review_report_card(*, actor: Membership, external_id: str, action: str, comment: str) -> ReportCard:
    item = _loaded_report_card(actor.school, external_id, lock=True)
    _assert_principal_secondary_authority(actor, item.academic_class)
    if item.state != ReportCardState.SUBMITTED:
        raise Rejected("Only a submitted report card can be reviewed.")
    if action == "approve":
        item.state = ReportCardState.REVIEWED
        item.principal_comment = comment.strip()
        item.reviewed_by = actor
        item.reviewed_at = timezone.now()
        item.version += 1
        item.save()
        _append_event(item, actor=actor, action="reviewed", comment=comment.strip())
    elif action == "return":
        if not comment.strip():
            raise Rejected("Add a comment before returning a report card with concerns.")
        item.state = ReportCardState.DRAFT
        item.submitted_by = None
        item.submitted_at = None
        item.version += 1
        item.save()
        _append_event(item, actor=actor, action="returned", comment=comment.strip())
    else:
        raise Rejected("Unsupported Principal report-card action.")
    return item


@transaction.atomic
def release_report_card(*, actor: Membership, external_id: str) -> ReportCard:
    _assert_manager_authority(actor)
    item = _loaded_report_card(actor.school, external_id, lock=True)
    # Non-Secondary classes have no Principal review step, so Administrator
    # may release straight from SUBMITTED; Secondary classes must pass
    # through Principal review first.
    allowed = {ReportCardState.REVIEWED}
    if item.academic_class.section.strip().casefold() != "secondary":
        allowed.add(ReportCardState.SUBMITTED)
    if item.state not in allowed:
        raise Rejected("This report card is not ready for release yet.")
    item.state = ReportCardState.RELEASED
    item.released_by = actor
    item.released_at = timezone.now()
    item.version += 1
    item.save()
    _append_event(item, actor=actor, action="released")
    return item


def _serialize_events(item: ReportCard) -> list[dict]:
    events = item.events.select_related("actor_membership__user").order_by("-revision")
    return [
        {
            "revision": event.revision,
            "action": event.action,
            "actorMembershipId": str(event.actor_membership_id),
            "actor": _membership_name(event.actor_membership),
            "comment": event.comment,
            "occurredAt": event.created_at.isoformat(),
        }
        for event in events
    ]


def serialize_report_card(item: ReportCard) -> dict:
    lines = item.lines.select_related("class_subject__subject").order_by("class_subject__subject__name")
    return {
        "id": item.external_id,
        "canonicalReportCardId": str(item.id),
        "studentId": item.student.student_code,
        "studentName": item.student.full_name,
        "admissionNumber": item.student.admission_number,
        "termId": str(item.term_id),
        "term": item.term.name,
        "className": item.academic_class.name,
        "section": item.academic_class.section,
        "state": item.state,
        "overallAverage": float(item.overall_average) if item.overall_average is not None else None,
        "overallGrade": item.overall_grade,
        "classPosition": item.class_position,
        "classSize": item.class_size,
        "attendancePercent": item.attendance_percent,
        "principalComment": item.principal_comment,
        "classTeacherComment": item.class_teacher_comment,
        "subjects": [
            {
                "classSubjectId": str(line.class_subject_id),
                "subject": line.class_subject.subject.name,
                "percent": float(line.weighted_percent) if line.weighted_percent is not None else None,
                "grade": line.grade,
                "assessmentsIncluded": line.assessments_included,
            }
            for line in lines
        ],
        "generatedAt": item.generated_at.isoformat() if item.generated_at else None,
        "submittedAt": item.submitted_at.isoformat() if item.submitted_at else None,
        "reviewedAt": item.reviewed_at.isoformat() if item.reviewed_at else None,
        "releasedAt": item.released_at.isoformat() if item.released_at else None,
        "version": item.version,
        "updatedAt": item.updated_at.isoformat(),
        # Same visibility as the report card itself - whoever may see the card
        # sees its full review history (Student/Parent only once released,
        # matching every other field here). A returned-then-fixed history is
        # ordinary transparency, not something to hide from a family.
        "events": _serialize_events(item),
    }


def _sync_record(*, school, entity_id, payload, actor=None):
    record = (
        SyncRecord.objects.select_for_update()
        .filter(school=school, entity_type=REPORT_CARD_ENTITY, entity_id=entity_id)
        .first()
    )
    if record is None:
        return SyncRecord.objects.create(
            school=school,
            entity_type=REPORT_CARD_ENTITY,
            entity_id=entity_id,
            payload=payload,
            version=1,
            deleted=False,
            updated_by=actor,
        )
    if record.payload == payload and not record.deleted:
        return record
    record.payload = payload
    record.deleted = False
    record.version += 1
    record.updated_by = actor
    record.save(update_fields=["payload", "deleted", "version", "updated_by"])
    return record


def publish_report_card_sync(item: ReportCard, *, actor=None):
    item = _loaded_report_card(item.school, item.external_id)
    return _sync_record(
        school=item.school,
        entity_id=item.external_id,
        payload=serialize_report_card(item),
        actor=actor,
    )


def replace_current_sync_payload(*, school, entity_id, payload, actor=None):
    SyncRecord.objects.filter(
        school=school, entity_type=REPORT_CARD_ENTITY, entity_id=entity_id
    ).update(payload=payload, deleted=False, updated_by=actor)
