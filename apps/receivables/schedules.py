"""Fee schedules: drafting, publishing, and correcting what has been published.

Only someone with billing authority may do any of it (checked here, not only in the API, and only for
their own school). A draft can be edited freely. PUBLISHING turns it into charges - one receivable per
student per fee item (or per instalment) - and after that the schedule is frozen: a mistake is put right
with adjustments, a void, or a new schedule, never by rewriting what families were told they owe.

Publishing is idempotent. The database allows one receivable per item, student and instalment, so
running it again (or a second time at once) creates nothing twice. It can safely be re-run later to
charge a student who joined after publication or whose family was only just set up.
"""

import re
import uuid
from dataclasses import dataclass, field
from datetime import date

from django.db import transaction
from django.utils import timezone

from apps.academics.models import AcademicClass, AcademicSession, AcademicTerm

from . import applicability, audit, plans
from .constants import DEFAULT_CURRENCY, MAX_AMOUNT_MINOR
from .errors import Refused
from .models import (
    FamilyStudent, FeeCategory, FeeItem, FeeSchedule, FeeScope, ReceivableStatus, ScheduleStatus, StudentReceivable,
)
from .permissions import can_manage_billing

_CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")
_ITEM_FIELDS = (
    "code", "name", "category", "amount_minor", "is_mandatory", "due_date", "plan", "sort_order", "scope",
    "section", "academic_class", "student", "metadata",
)


def require_billing_authority(actor, school) -> None:
    """The actor must be a billing authority AT THIS SCHOOL: a membership from another school never counts."""
    if actor is None or actor.school_id != school.id or not can_manage_billing(actor):
        raise Refused(
            "Only the owner, or someone the owner has given billing authority, can decide what families owe.",
            "not_billing_authority",
        )


def _name(value, what: str) -> str:
    cleaned = " ".join(str(value or "").split())
    if not cleaned:
        raise Refused(f"Give the {what} a name.", "name_required")
    if len(cleaned) > 120:
        raise Refused(f"A {what} name can be at most 120 characters.", "name_too_long")
    return cleaned


def _require_draft(schedule: FeeSchedule) -> None:
    if schedule.status != ScheduleStatus.DRAFT:
        raise Refused(
            "This schedule is published, so it cannot be edited. Correct it with adjustments, a void or a new schedule.",
            "schedule_not_draft",
        )


def _dupe_name(school, session, term, name, exclude=None) -> bool:
    live = FeeSchedule.objects.filter(school=school, session=session, term=term, name=name).exclude(status=ScheduleStatus.RETIRED)
    return live.exclude(pk=exclude.pk).exists() if exclude else live.exists()


# -- drafting -------------------------------------------------------------------------------------


@transaction.atomic
def create_schedule(school, *, session: AcademicSession, name, actor, term: AcademicTerm | None = None, replaces=None) -> FeeSchedule:
    require_billing_authority(actor, school)
    if session.school_id != school.id:
        raise Refused("That academic session is not at this school.", "session_not_found")
    if term is not None and term.session_id != session.id:
        raise Refused("That term is not in the chosen session.", "term_not_in_session")
    if replaces is not None and replaces.school_id != school.id:
        raise Refused("That schedule is not at this school.", "schedule_not_found")
    name = _name(name, "schedule")
    if _dupe_name(school, session, term, name):
        raise Refused("A schedule with that name already exists for this session and term.", "schedule_exists")
    schedule = FeeSchedule.objects.create(school=school, session=session, term=term, name=name, created_by=actor, replaces=replaces)
    audit.record(school, "fee_schedule_created", actor=actor, obj=schedule, name=name, session=str(session.id), term=str(term.id) if term else "")
    return schedule


@transaction.atomic
def rename_schedule(schedule: FeeSchedule, name, *, actor) -> FeeSchedule:
    require_billing_authority(actor, schedule.school)
    schedule = FeeSchedule.objects.select_for_update().get(pk=schedule.pk)  # what is stored, not a stale copy
    _require_draft(schedule)
    name = _name(name, "schedule")
    if _dupe_name(schedule.school, schedule.session, schedule.term, name, exclude=schedule):
        raise Refused("A schedule with that name already exists for this session and term.", "schedule_exists")
    before, schedule.name = schedule.name, name
    schedule.save(update_fields=["name", "updated_at"])
    audit.record(schedule.school, "fee_schedule_renamed", actor=actor, obj=schedule, before=before, after=name)
    return schedule


def _clean_item(schedule: FeeSchedule, fields: dict, existing: FeeItem | None = None) -> dict:
    unknown = set(fields) - set(_ITEM_FIELDS)
    if unknown:
        raise Refused(f"'{sorted(unknown)[0]}' is not something a fee item has.", "unexpected_field")
    merged = {name: getattr(existing, name) for name in _ITEM_FIELDS} if existing else {
        "category": FeeCategory.OTHER, "is_mandatory": True, "due_date": None, "plan": [], "sort_order": 0,
        "scope": FeeScope.ALL, "section": "", "academic_class": None, "student": None, "metadata": {},
    }
    merged.update(fields)

    code = str(merged.get("code") or "").strip()
    if not _CODE.match(code):
        raise Refused("An item code is letters, digits, dashes or underscores (up to 40).", "invalid_code")
    name = _name(merged.get("name"), "fee item")
    if merged["category"] not in FeeCategory.values:
        raise Refused("That is not a fee category.", "invalid_category")
    amount = merged.get("amount_minor")
    if isinstance(amount, bool) or not isinstance(amount, int) or not 0 < amount <= MAX_AMOUNT_MINOR:
        raise Refused("The amount must be a whole number of kobo greater than zero.", "invalid_amount")
    if not isinstance(merged["is_mandatory"], bool):
        raise Refused("Say whether the item is mandatory.", "invalid_mandatory")
    if isinstance(merged["sort_order"], bool) or not isinstance(merged["sort_order"], int) or not 0 <= merged["sort_order"] <= 1000:
        raise Refused("The order must be a whole number from 0 to 1000.", "invalid_order")
    if not isinstance(merged["metadata"], dict):
        raise Refused("Notes on an item must be a dictionary.", "invalid_metadata")

    due = merged.get("due_date")
    if isinstance(due, str):
        try:
            due = date.fromisoformat(due)
        except ValueError:
            raise Refused("The due date is not a valid date.", "invalid_due_date")
    if due is not None and not isinstance(due, date):
        raise Refused("The due date is not a valid date.", "invalid_due_date")
    plan = plans.clean_plan(merged.get("plan"))
    if plan and min(plans.split(amount, [p["basisPoints"] for p in plan])) < 1:
        raise Refused("The amount is too small to split that way: an instalment would be nothing.", "invalid_plan")

    scope = merged["scope"]
    if scope not in FeeScope.values:
        raise Refused("That is not a way to aim a fee.", "invalid_scope")
    section, academic_class, student = str(merged["section"] or "").strip(), merged["academic_class"], merged["student"]
    if scope == FeeScope.ALL and (section or academic_class or student):
        raise Refused("A fee for everyone names no section, class or student.", "invalid_scope")
    if scope == FeeScope.SECTION:
        known = {c.section.strip().casefold() for c in AcademicClass.objects.filter(school=schedule.school)}
        if not section or section.casefold() not in known or academic_class or student:
            raise Refused("Choose one of the school's academic sections.", "invalid_section")
    if scope == FeeScope.CLASS:
        if academic_class is None or academic_class.school_id != schedule.school_id or section or student:
            raise Refused("Choose one of the school's classes.", "invalid_class")
    if scope == FeeScope.STUDENT:
        if student is None or student.school_id != schedule.school_id or section or academic_class:
            raise Refused("Choose a student at this school.", "invalid_student")
    if not merged["is_mandatory"] and scope != FeeScope.STUDENT:
        raise Refused("An optional item is charged only to the students it is aimed at: aim it at a student.", "optional_needs_student")

    return {
        "code": code, "name": name, "category": merged["category"], "amount_minor": amount, "currency": DEFAULT_CURRENCY,
        "is_mandatory": merged["is_mandatory"], "due_date": due, "plan": plan, "sort_order": merged["sort_order"],
        "scope": scope, "section": section if scope == FeeScope.SECTION else "",
        "academic_class": academic_class if scope == FeeScope.CLASS else None,
        "student": student if scope == FeeScope.STUDENT else None, "metadata": merged["metadata"],
    }


@transaction.atomic
def add_item(schedule: FeeSchedule, *, actor, **fields) -> FeeItem:
    require_billing_authority(actor, schedule.school)
    schedule = FeeSchedule.objects.select_for_update().get(pk=schedule.pk)
    _require_draft(schedule)
    cleaned = _clean_item(schedule, fields)
    if schedule.items.filter(code=cleaned["code"]).exists():
        raise Refused("This schedule already has an item with that code.", "item_exists")
    item = FeeItem.objects.create(schedule=schedule, **cleaned)
    audit.record(schedule.school, "fee_item_added", actor=actor, obj=item, schedule=str(schedule.id), code=item.code, amount=item.amount_minor)
    return item


@transaction.atomic
def update_item(item: FeeItem, *, actor, **fields) -> FeeItem:
    schedule = item.schedule
    require_billing_authority(actor, schedule.school)
    schedule = FeeSchedule.objects.select_for_update().get(pk=schedule.pk)
    _require_draft(schedule)
    before = {"amount": item.amount_minor, "code": item.code, "name": item.name}
    cleaned = _clean_item(schedule, fields, existing=item)
    if schedule.items.filter(code=cleaned["code"]).exclude(pk=item.pk).exists():
        raise Refused("This schedule already has an item with that code.", "item_exists")
    for name, value in cleaned.items():
        setattr(item, name, value)
    item.save()
    audit.record(schedule.school, "fee_item_changed", actor=actor, obj=item, schedule=str(schedule.id), before=before, after={"amount": item.amount_minor, "code": item.code, "name": item.name})
    return item


@transaction.atomic
def remove_item(item: FeeItem, *, actor) -> None:
    schedule = item.schedule
    require_billing_authority(actor, schedule.school)
    schedule = FeeSchedule.objects.select_for_update().get(pk=schedule.pk)
    _require_draft(schedule)
    audit.record(schedule.school, "fee_item_removed", actor=actor, obj=item, schedule=str(schedule.id), code=item.code, amount=item.amount_minor)
    item.delete()


def problems(schedule: FeeSchedule) -> list[str]:
    """Everything that stops this draft being published, in words."""
    found = []
    items = list(schedule.items.all())
    if not items:
        found.append("Add at least one fee item.")
    for item in items:
        if not item.plan and item.due_date is None:
            found.append(f"'{item.name}' has no due date.")
        if item.scope == FeeScope.CLASS and not item.academic_class.is_active:
            found.append(f"'{item.name}' is aimed at a class that is no longer active.")
    return found


# -- publishing -----------------------------------------------------------------------------------


@dataclass
class PublishReport:
    created: int = 0
    already_existed: int = 0
    families: set = field(default_factory=set)
    #: students the schedule applies to who have no family, so cannot be charged yet
    without_family: list = field(default_factory=list)
    #: students who could not be placed in a class for the session, so no class or section fee reached them
    unclassified: list = field(default_factory=list)
    #: charges a replacement schedule left alone because the schedule it replaces already charged that student for that item
    already_charged_by_replaced: int = 0

    @property
    def complete(self) -> bool:
        return not self.without_family and not self.unclassified


def _replaced_schedule_ids(schedule: FeeSchedule) -> list:
    found, current = [], schedule.replaces
    while current is not None and len(found) < 10:
        found.append(current.id)
        current = current.replaces
    return found


def _materialise(schedule: FeeSchedule, actor) -> PublishReport:
    """Charge everyone the schedule applies to. Safe to run again: it creates only what is missing.

    A schedule that REPLACES another never charges a student again for an item the replaced schedule
    already charged (and that has not been voided): correcting an amount already charged is an
    adjustment, not a second charge."""
    report = PublishReport()
    replaced = _replaced_schedule_ids(schedule)
    charged_before = set(
        StudentReceivable.objects.filter(schedule_id__in=replaced).exclude(status=ReceivableStatus.VOID).values_list("student_id", "item_code")
    ) if replaced else set()
    resolution = applicability.resolve(schedule)
    report.unclassified = resolution.unclassified
    family_of = {
        m.student_id: m.family
        for m in FamilyStudent.objects.select_related("family").filter(school=schedule.school, is_active=True)
        if m.family.status == "active"
    }
    missing_family: dict = {}
    for item in schedule.items.all():
        existing = {}
        for student_id, number, key in StudentReceivable.objects.filter(fee_item=item).values_list("student_id", "installment_number", "charge_key"):
            existing.setdefault(student_id, {})[number] = key
        rows = []
        for student in resolution.by_item.get(item.id, []):
            family = family_of.get(student.id)
            if family is None:
                missing_family[student.id] = student
                continue
            if (student.id, item.code) in charged_before:
                report.already_charged_by_replaced += 1
                continue
            have = existing.get(student.id, {})
            # Every instalment of one charge shares a key, including any added on a later refresh.
            charge_key = next(iter(have.values()), None) or uuid.uuid4()
            parts = plans.parts_for(item.amount_minor, item.due_date, item.plan)
            for number, due, amount in parts:
                if number in have:
                    report.already_existed += 1
                    continue
                rows.append(StudentReceivable(
                    school=schedule.school, student=student, family=family, schedule=schedule, fee_item=item,
                    session=schedule.session, term=schedule.term, item_code=item.code, item_name=item.name,
                    item_category=item.category, currency=item.currency, gross_amount_minor=amount, due_date=due,
                    installment_number=number, installment_count=len(parts), charge_key=charge_key, published_by=actor,
                ))
                report.families.add(family.id)
        # A database rule (one receivable per item, student and instalment) makes a race harmless.
        StudentReceivable.objects.bulk_create(rows, ignore_conflicts=True)
        report.created += len(rows)
    report.without_family = sorted(missing_family.values(), key=lambda s: (s.surname, s.first_name, str(s.id)))
    return report


@transaction.atomic
def publish(schedule: FeeSchedule, *, actor) -> PublishReport:
    """Make a draft real: fix it, and charge every student it applies to."""
    require_billing_authority(actor, schedule.school)
    schedule = FeeSchedule.objects.select_for_update().get(pk=schedule.pk)
    if schedule.status != ScheduleStatus.DRAFT:
        raise Refused("This schedule has already been published.", "already_published")
    if schedule.replaces_id and schedule.replaces.status != ScheduleStatus.RETIRED:
        raise Refused("Retire the schedule this one replaces before publishing it, so nothing is charged twice.", "replaced_not_retired")
    found = problems(schedule)
    if found:
        raise Refused(" ".join(found), "not_ready")
    schedule.status = ScheduleStatus.PUBLISHED
    schedule.published_by = actor
    schedule.published_at = timezone.now()
    schedule.save(update_fields=["status", "published_by", "published_at", "updated_at"])
    report = _materialise(schedule, actor)
    audit.record(
        schedule.school, "fee_schedule_published", actor=actor, obj=schedule, name=schedule.name,
        charges=report.created, families=len(report.families), without_family=len(report.without_family),
        unclassified=len(report.unclassified),
    )
    from . import lifecycle

    lifecycle.after_new_charges(schedule, report, actor)
    return report


@transaction.atomic
def refresh(schedule: FeeSchedule, *, actor) -> PublishReport:
    """Charge whoever the published schedule applies to who has not been charged yet - a student who joined
    after publication, or one whose family has just been set up. Never touches an existing charge."""
    require_billing_authority(actor, schedule.school)
    schedule = FeeSchedule.objects.select_for_update().get(pk=schedule.pk)
    if schedule.status != ScheduleStatus.PUBLISHED:
        raise Refused("Only a published schedule can be refreshed.", "not_published")
    report = _materialise(schedule, actor)
    if report.created:
        audit.record(schedule.school, "fee_schedule_refreshed", actor=actor, obj=schedule, charges=report.created, families=len(report.families))
        from . import lifecycle

        lifecycle.after_new_charges(schedule, report, actor, announce=False)
    return report


# -- correcting what was published -----------------------------------------------------------------


@transaction.atomic
def retire(schedule: FeeSchedule, *, actor, reason) -> FeeSchedule:
    """Close a schedule: it can no longer be refreshed. Charges already made stay exactly as they are."""
    require_billing_authority(actor, schedule.school)
    schedule = FeeSchedule.objects.select_for_update().get(pk=schedule.pk)
    reason = " ".join(str(reason or "").split())
    if not reason:
        raise Refused("Say why the schedule is being retired.", "reason_required")
    if schedule.status == ScheduleStatus.RETIRED:
        raise Refused("This schedule is already retired.", "already_retired")
    schedule.status, schedule.retired_by, schedule.retired_at, schedule.retire_reason = (
        ScheduleStatus.RETIRED, actor, timezone.now(), reason[:300],
    )
    schedule.save(update_fields=["status", "retired_by", "retired_at", "retire_reason", "updated_at"])
    audit.record(schedule.school, "fee_schedule_retired", actor=actor, obj=schedule, reason=reason)
    return schedule


@transaction.atomic
def clone(schedule: FeeSchedule, *, actor, name=None) -> FeeSchedule:
    """A new draft with the same items, for correcting or following a schedule. Nothing is charged until it is published."""
    require_billing_authority(actor, schedule.school)
    copy = create_schedule(
        schedule.school, session=schedule.session, term=schedule.term, actor=actor, replaces=schedule,
        name=name or f"{schedule.name} (copy)",
    )
    for item in schedule.items.all():
        FeeItem.objects.create(
            schedule=copy, code=item.code, name=item.name, category=item.category, amount_minor=item.amount_minor,
            currency=item.currency, is_mandatory=item.is_mandatory, due_date=item.due_date, plan=item.plan,
            sort_order=item.sort_order, scope=item.scope, section=item.section, academic_class=item.academic_class,
            student=item.student, metadata=item.metadata,
        )
    return copy
