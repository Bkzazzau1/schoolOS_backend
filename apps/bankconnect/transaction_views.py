"""Reading payments and working the review queue. Reading is for the owner and the finance office;
so is deciding what a payment is, because that is the finance office's daily work."""

from django.db.models import Count, Prefetch, Q
from django.utils.dateparse import parse_date
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.students.models import EnrollmentStatus, Student, StudentEnrollment

from . import review, summary
from .constants import NEEDS_A_PERSON, Direction, ReconStatus
from .http import bad_filter, bank_errors, body, is_uuid
from .models import BankTransaction, TransactionAllocation
from .permissions import acting_membership
from .serializers import serialize_transaction, serialize_transaction_detail

MAX_PAGE = 100


def _payments(membership):
    active = Prefetch(
        "allocations",
        queryset=TransactionAllocation.objects.filter(superseded=False).select_related("student"),
        to_attr="active_allocations",
    )
    return BankTransaction.objects.filter(school=membership.school).prefetch_related(active)


def _filtered(rows, params):
    """The rows narrowed by the query string, or a 400 Response naming the filter that was wrong."""
    if params.get("connection"):
        if not is_uuid(params["connection"]):
            return bad_filter("connection")
        rows = rows.filter(connection_id=params["connection"])
    if params.get("status"):
        if params["status"] not in ReconStatus.values:
            return bad_filter("status")
        rows = rows.filter(reconciliation_status=params["status"])
    if params.get("direction"):
        if params["direction"] not in Direction.values:
            return bad_filter("direction")
        rows = rows.filter(direction=params["direction"])
    for name, lookup in (("from", "transaction_date__date__gte"), ("to", "transaction_date__date__lte")):
        if params.get(name):
            day = parse_date(params[name])
            if day is None:
                return bad_filter(name)
            rows = rows.filter(**{lookup: day})
    if params.get("q"):
        term = params["q"].strip()[:60]
        rows = rows.filter(
            Q(sender_name__icontains=term) | Q(narration__icontains=term) | Q(transaction_reference__icontains=term)
        )
    if params.get("sandbox") == "exclude":
        rows = rows.filter(is_sandbox=False)
    return rows


def _paged(rows, params, extra=None):
    try:
        limit = min(max(int(params.get("limit", 50)), 1), MAX_PAGE)
        offset = max(int(params.get("offset", 0)), 0)
    except ValueError:
        return bad_filter("limit")
    total = rows.count()
    page = list(rows[offset : offset + limit])
    return Response(
        {"transactions": [serialize_transaction(t) for t in page], "total": total, "hasMore": offset + limit < total, **(extra or {})}
    )


class TransactionsView(APIView):
    """This school's payments, newest first, across every connected account."""

    def get(self, request, school_id):
        membership = acting_membership(request, school_id)
        rows = _filtered(_payments(membership), request.query_params)
        return rows if isinstance(rows, Response) else _paged(rows, request.query_params)


class TransactionDetailView(APIView):
    def get(self, request, school_id, transaction_id):
        membership = acting_membership(request, school_id)
        row = _payments(membership).filter(id=transaction_id).first()
        if row is None:
            raise NotFound("That payment was not found.")
        return Response({"transaction": serialize_transaction_detail(row)})


class ReviewQueueView(APIView):
    """Money received that a person still has something to do with, oldest first."""

    def get(self, request, school_id):
        membership = acting_membership(request, school_id)
        queue = _payments(membership).filter(direction=Direction.CREDIT, reconciliation_status__in=NEEDS_A_PERSON)
        if request.query_params.get("sandbox") == "exclude":
            queue = queue.filter(is_sandbox=False)
        counts = {
            row["reconciliation_status"]: row["n"]
            for row in queue.order_by().values("reconciliation_status").annotate(n=Count("id"))
        }
        params = request.query_params.copy()
        params.pop("sandbox", None)  # already applied to the counts and the queue
        rows = _filtered(queue, params)
        if isinstance(rows, Response):
            return rows
        return _paged(rows.order_by("transaction_date", "created_at"), params, {"counts": counts})


class DecideView(APIView):
    """A person's decision on one payment. See `review` for the rules."""

    @bank_errors
    def post(self, request, school_id, transaction_id):
        membership = acting_membership(request, school_id)
        data = body(request)
        review.decide(
            membership, transaction_id,
            action=data.get("action"), expected_status=data.get("expectedStatus"),
            student_id=data.get("studentId"), purpose=data.get("purpose"), allocations=data.get("allocations"),
            note=data.get("note"), duplicate_of=data.get("duplicateOf"),
        )
        row = _payments(membership).get(id=transaction_id)
        return Response({"transaction": serialize_transaction_detail(row)})


class StudentSearchView(APIView):
    """Find a student to assign a payment to, by name, student code or admission number."""

    def get(self, request, school_id):
        membership = acting_membership(request, school_id)
        term = request.query_params.get("q", "").strip()[:60]
        if len(term) < 2:
            return Response({"students": []})
        found = list(
            Student.objects.filter(school=membership.school).filter(
                Q(first_name__icontains=term) | Q(surname__icontains=term) | Q(other_name__icontains=term)
                | Q(student_code__icontains=term) | Q(admission_number__icontains=term)
            )[:20]
        )
        classes = dict(
            StudentEnrollment.objects.filter(student__in=found, status=EnrollmentStatus.ACTIVE)
            .order_by("started_at").values_list("student_id", "class_name")
        )
        return Response(
            {
                "students": [
                    {
                        "id": str(s.id), "name": s.full_name, "studentCode": s.student_code,
                        "admissionNumber": s.admission_number, "className": classes.get(s.id, ""), "status": s.status,
                    }
                    for s in found
                ]
            }
        )


class SummaryView(APIView):
    """How much has come in, by day, week and term, by purpose and bank, and how much is reconciled."""

    def get(self, request, school_id):
        membership = acting_membership(request, school_id)
        period = request.query_params.get("period", "term")
        if period not in summary.PERIODS:
            return bad_filter("period")
        include_sandbox = request.query_params.get("includeSandbox") == "true"
        return Response({"summary": summary.build(membership.school, period=period, include_sandbox=include_sandbox)})
