"""Fee schedules, the charges they raise, and the decisions about them. Reading is for the owner and the
finance office; changing what families owe (drafting, publishing, adjustments, voids) is for a billing
authority only - the service checks that again, so this is never the only guard."""

from rest_framework.response import Response

from apps.academics.models import AcademicClass, AcademicSession, AcademicTerm
from apps.bankconnect.models import BankTransaction
from apps.students.models import Student

from . import adjustments, allocation, ledger, schedules, serializers
from .errors import Refused
from .http import ReceivablesView, body, found, paging, uuid_arg
from .models import FeeItem, FeeSchedule, ReceivableAdjustment, StudentReceivable
from .permissions import acting_membership


def _schedule(membership, schedule_id) -> FeeSchedule:
    return found(FeeSchedule.objects.filter(school=membership.school, id=schedule_id), "fee schedule")


def _item_fields(school, data: dict) -> dict:
    """The item fields the request names, turned into what the service takes. Unnamed fields are left alone."""
    names = {"code": "code", "name": "name", "category": "category", "amountMinor": "amount_minor", "isMandatory": "is_mandatory",
             "dueDate": "due_date", "plan": "plan", "sortOrder": "sort_order", "scope": "scope", "section": "section", "metadata": "metadata"}
    fields = {names[k]: v for k, v in data.items() if k in names}
    unknown = set(data) - set(names) - {"academicClassId", "studentId"}
    if unknown:
        raise Refused(f"'{sorted(unknown)[0]}' is not something a fee item has.", "unexpected_field")
    if "academicClassId" in data:
        fields["academic_class"] = found(AcademicClass.objects.filter(school=school, id=uuid_arg(data["academicClassId"], "class")), "class") if data["academicClassId"] else None
    if "studentId" in data:
        fields["student"] = found(Student.objects.filter(school=school, id=uuid_arg(data["studentId"], "student")), "student") if data["studentId"] else None
    return fields


class FeeSchedulesView(ReceivablesView):
    def get(self, request, school_id):
        membership = acting_membership(request, school_id)
        rows = FeeSchedule.objects.filter(school=membership.school)
        if request.query_params.get("status"):
            rows = rows.filter(status=request.query_params["status"])
        return Response({"schedules": [serializers.schedule(s) for s in rows[:200]]})

    def post(self, request, school_id):
        membership = acting_membership(request, school_id, manage=True)
        data = body(request)
        session = found(AcademicSession.objects.filter(school=membership.school, id=uuid_arg(data.get("sessionId"), "session")), "session")
        term = found(AcademicTerm.objects.filter(session=session, id=uuid_arg(data.get("termId"), "term")), "term") if data.get("termId") else None
        schedule = schedules.create_schedule(membership.school, session=session, term=term, name=data.get("name"), actor=membership)
        return Response({"schedule": serializers.schedule(schedule, items=True)}, status=201)


class FeeScheduleDetailView(ReceivablesView):
    def get(self, request, school_id, schedule_id):
        membership = acting_membership(request, school_id)
        return Response({"schedule": serializers.schedule(_schedule(membership, schedule_id), items=True)})


class FeeSchedulePreviewView(ReceivablesView):
    """Who the schedule would charge, and how much - without charging anyone."""

    def get(self, request, school_id, schedule_id):
        membership = acting_membership(request, school_id)
        result = schedules.preview(_schedule(membership, schedule_id))
        return Response({
            "preview": {
                "items": result["items"], "totalMinor": result["totalMinor"],
                "withoutFamily": [serializers.student_brief(s) for s in result["withoutFamily"]],
                "unclassified": [serializers.student_brief(s) for s in result["unclassified"]],
                "problems": schedules.problems(_schedule(membership, schedule_id)),
            }
        })


class FeeScheduleActionView(ReceivablesView):
    action = None

    def post(self, request, school_id, schedule_id):
        membership = acting_membership(request, school_id, manage=True)
        schedule, data = _schedule(membership, schedule_id), body(request)
        if self.action == "rename":
            schedules.rename_schedule(schedule, data.get("name"), actor=membership)
        elif self.action == "publish":
            report = schedules.publish(schedule, actor=membership)
            schedule.refresh_from_db()
            return Response({"schedule": serializers.schedule(schedule, items=True), "report": serializers.publish_report(report)})
        elif self.action == "refresh":
            report = schedules.refresh(schedule, actor=membership)
            return Response({"schedule": serializers.schedule(schedule, items=True), "report": serializers.publish_report(report)})
        elif self.action == "retire":
            schedules.retire(schedule, actor=membership, reason=data.get("reason"))
        elif self.action == "clone":
            copy = schedules.clone(schedule, actor=membership, name=data.get("name"))
            return Response({"schedule": serializers.schedule(copy, items=True)}, status=201)
        elif self.action == "void-charges":
            result = adjustments.void_schedule_charges(schedule, actor=membership, reason=data.get("reason"), include_paid=bool(data.get("includePaid")))
            return Response({"voided": result["voided"], "skippedPaid": [str(r.id) for r in result["skipped_paid"]]})
        schedule.refresh_from_db()
        return Response({"schedule": serializers.schedule(schedule, items=True)})


class FeeItemsView(ReceivablesView):
    def post(self, request, school_id, schedule_id):
        membership = acting_membership(request, school_id, manage=True)
        schedule = _schedule(membership, schedule_id)
        item = schedules.add_item(schedule, actor=membership, **_item_fields(membership.school, body(request)))
        return Response({"item": serializers.item(item)}, status=201)


class FeeItemActionView(ReceivablesView):
    action = None

    def post(self, request, school_id, schedule_id, item_id):
        membership = acting_membership(request, school_id, manage=True)
        item = found(FeeItem.objects.filter(school=membership.school, schedule_id=schedule_id, id=item_id), "fee item")
        if self.action == "remove":
            schedules.remove_item(item, actor=membership)
            return Response({"removed": True})
        item = schedules.update_item(item, actor=membership, **_item_fields(membership.school, body(request)))
        return Response({"item": serializers.item(item)})


class ReceivablesView(ReceivablesView):
    """The school's charges. Filter by ?student=, ?family=, ?schedule=, ?status=."""

    def get(self, request, school_id):
        membership = acting_membership(request, school_id)
        rows = StudentReceivable.objects.filter(school=membership.school).select_related("student")
        for name, field in (("student", "student_id"), ("family", "family_id"), ("schedule", "schedule_id")):
            if request.query_params.get(name):
                rows = rows.filter(**{field: uuid_arg(request.query_params[name], name)})
        if request.query_params.get("status"):
            rows = rows.filter(status=request.query_params["status"])
        limit, offset = paging(request)
        total = rows.count()
        page = list(rows.order_by("due_date", "created_at", "id")[offset: offset + limit])
        figures = ledger.positions(page)
        return Response({"receivables": [serializers.receivable(r, figures[r.id]) for r in page], "total": total, "hasMore": offset + limit < total})


class ReceivableDetailView(ReceivablesView):
    def get(self, request, school_id, receivable_id):
        membership = acting_membership(request, school_id)
        r = found(StudentReceivable.objects.filter(school=membership.school, id=receivable_id).select_related("student"), "charge")
        body_ = serializers.receivable(r, ledger.position(r))
        body_["adjustments"] = [serializers.adjustment(a) for a in r.adjustments.all()]
        body_["allocations"] = [serializers.allocation_row(a) for a in r.allocations.order_by("created_at", "id")]
        return Response({"receivable": body_})


class ReceivableActionView(ReceivablesView):
    action = None

    def post(self, request, school_id, receivable_id):
        membership = acting_membership(request, school_id, manage=True)
        r = found(StudentReceivable.objects.filter(school=membership.school, id=receivable_id).select_related("student"), "charge")
        data = body(request)
        if self.action == "adjust":
            method = adjustments.adjust_charge if data.get("wholeCharge") else adjustments.adjust
            made = method(r, kind=data.get("kind"), amount_minor=data.get("amountMinor"), reason=data.get("reason"), actor=membership)
            made = made if isinstance(made, list) else [made]
            r.refresh_from_db()
            return Response({"adjustments": [serializers.adjustment(a) for a in made], "receivable": serializers.receivable(r, ledger.position(r))}, status=201)
        r = adjustments.void_receivable(r, actor=membership, reason=data.get("reason"))
        return Response({"receivable": serializers.receivable(r, ledger.position(r))})


class AdjustmentsView(ReceivablesView):
    """The history of decisions about what families owe. Filter by ?receivable=."""

    def get(self, request, school_id):
        membership = acting_membership(request, school_id)
        rows = ReceivableAdjustment.objects.filter(school=membership.school)
        if request.query_params.get("receivable"):
            rows = rows.filter(receivable_id=uuid_arg(request.query_params["receivable"], "charge"))
        limit, offset = paging(request)
        total = rows.count()
        return Response({"adjustments": [serializers.adjustment(a) for a in rows.order_by("-created_at", "-id")[offset: offset + limit]], "total": total})


class AdjustmentReverseView(ReceivablesView):
    def post(self, request, school_id, adjustment_id):
        membership = acting_membership(request, school_id, manage=True)
        original = found(ReceivableAdjustment.objects.filter(school=membership.school, id=adjustment_id), "adjustment")
        reversal = adjustments.reverse(original, actor=membership, reason=body(request).get("reason"))
        return Response({"adjustment": serializers.adjustment(reversal)}, status=201)


class ReallocateView(ReceivablesView):
    """The finance office puts a payment exactly where it belongs. The old allocation is kept, superseded."""

    def post(self, request, school_id, transaction_id):
        membership = acting_membership(request, school_id)
        tx = found(BankTransaction.objects.filter(school=membership.school, id=transaction_id), "payment")
        data = body(request)
        plan = []
        for part in data.get("plan") or []:
            if not isinstance(part, dict):
                raise Refused("Each part of the plan needs a charge and an amount.", "invalid_plan")
            plan.append((found(StudentReceivable.objects.filter(school=membership.school, id=uuid_arg(part.get("receivableId"), "charge")), "charge"), part.get("amountMinor")))
        decision = allocation.decision_for(tx, action="reallocate", actor=membership, note=str(data.get("reason") or ""))
        result = allocation.correct_allocations(tx, plan, actor=membership, reason=data.get("reason"), decision=decision)
        return Response({"allocatedMinor": result.allocated_minor, "creditMinor": result.credit_minor, "allocations": [serializers.allocation_row(a) for a in result.allocations]})
