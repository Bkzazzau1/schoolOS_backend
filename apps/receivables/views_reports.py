"""The calendar the fee system follows, and what is owed by session and term. For the owner and the finance
office; everything is the school's own and derived from the ledger."""

from rest_framework.response import Response

from apps.academics.models import AcademicSession

from . import periods, reports
from .http import ReceivablesView, found, uuid_arg
from .permissions import acting_membership


def _term(t, current_id) -> dict:
    return {
        "id": str(t.id), "name": t.name, "code": t.code, "sequence": t.sequence, "startsOn": t.starts_on.isoformat(),
        "endsOn": t.ends_on.isoformat(), "status": t.status, "isCurrent": str(t.id) == current_id,
    }


class CalendarView(ReceivablesView):
    """The school's sessions and terms, and which are current: what a fee schedule can be made for."""

    def get(self, request, school_id):
        membership = acting_membership(request, school_id)
        current_session, current_term = periods.current_period(membership.school)
        current_term_id = str(current_term.id) if current_term else ""
        sessions = AcademicSession.objects.filter(school=membership.school).prefetch_related("terms").order_by("-starts_on")
        return Response({
            "current": {
                "sessionId": str(current_session.id) if current_session else None, "termId": current_term_id or None,
                "label": periods.label(current_session, current_term) if current_session else "",
            },
            "sessions": [
                {
                    "id": str(s.id), "name": s.name, "code": s.code, "startsOn": s.starts_on.isoformat(), "endsOn": s.ends_on.isoformat(),
                    "status": s.status, "isCurrent": current_session is not None and s.id == current_session.id,
                    "terms": [_term(t, current_term_id) for t in s.terms.all()],
                }
                for s in sessions
            ],
            "dueDateLeadDays": periods.DUE_LEAD_DAYS,
        })


class TermReportView(ReceivablesView):
    """GET what was billed, taken off, paid and is still owed, term by term. Optional ?session=<id>."""

    def get(self, request, school_id):
        membership = acting_membership(request, school_id)
        session = None
        if request.query_params.get("session"):
            session = found(AcademicSession.objects.filter(school=membership.school, id=uuid_arg(request.query_params["session"], "session")), "session")
        return Response({"report": reports.by_term(membership.school, session=session)})


class PositionView(ReceivablesView):
    """GET what the school is owed right now, in total and by period."""

    def get(self, request, school_id):
        membership = acting_membership(request, school_id)
        return Response({"position": reports.school_position(membership.school)})
