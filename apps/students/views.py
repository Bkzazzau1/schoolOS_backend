from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.permissions import require_membership
from apps.schools.models import Role

from .models import AdmissionApplication, Student, StudentStatus
from .services import serialize_admission, serialize_roster_summary, serialize_student


_ROSTER_READ_ROLES = {
    Role.PROPRIETOR,
    Role.ADMINISTRATOR,
    Role.PRINCIPAL,
}
_ADMISSIONS_READ_ROLES = {
    Role.PROPRIETOR,
    Role.ADMINISTRATOR,
}


def _membership(request, school_id, *, roles):
    return require_membership(
        request.user,
        school_id,
        roles=roles,
        membership_id=request.query_params.get("membership"),
    )


class SchoolStudentListView(APIView):
    """Read the canonical student roster for one school.

    Writes remain offline-first through the sync outbox. This endpoint is a
    server-authoritative read surface for administration and future web clients.
    """

    def get(self, request, school_id):
        membership = _membership(request, school_id, roles=_ROSTER_READ_ROLES)
        students = Student.objects.filter(school=membership.school).prefetch_related(
            "enrollments",
            "guardians",
        )
        requested_status = request.query_params.get("status")
        if requested_status:
            if requested_status not in StudentStatus.values:
                raise PermissionDenied("That student status is not available.")
            students = students.filter(status=requested_status)
        students = students.order_by("surname", "first_name", "student_code")
        return Response(
            {
                "students": [serialize_student(student) for student in students],
                "roster": serialize_roster_summary(membership.school),
            }
        )


class SchoolStudentDetailView(APIView):
    def get(self, request, school_id, student_id):
        membership = _membership(request, school_id, roles=_ROSTER_READ_ROLES)
        try:
            student = Student.objects.prefetch_related(
                "enrollments",
                "guardians",
                "lifecycle_events",
            ).get(id=student_id, school=membership.school)
        except Student.DoesNotExist as exc:
            raise PermissionDenied("This student is not available to this school.") from exc
        payload = serialize_student(student)
        payload["enrollmentHistory"] = [
            {
                "id": str(enrollment.id),
                "academicSection": enrollment.academic_section,
                "className": enrollment.class_name,
                "status": enrollment.status,
                "billable": enrollment.is_billable,
                "startedAt": enrollment.started_at.isoformat(),
                "endedAt": enrollment.ended_at.isoformat() if enrollment.ended_at else None,
            }
            for enrollment in student.enrollments.order_by("-started_at", "-id")
        ]
        payload["lifecycleHistory"] = [
            {
                "id": event.external_id,
                "workflow": event.workflow,
                "change": event.change,
                "status": event.status,
                "fromClass": event.from_class,
                "toClass": event.to_class,
                "requestedAt": event.requested_at.isoformat(),
                "completedAt": event.completed_at.isoformat() if event.completed_at else None,
                "approvedBy": event.approved_by,
            }
            for event in student.lifecycle_events.order_by("-requested_at", "-id")
        ]
        return Response(payload)


class SchoolAdmissionsListView(APIView):
    def get(self, request, school_id):
        membership = _membership(request, school_id, roles=_ADMISSIONS_READ_ROLES)
        applications = AdmissionApplication.objects.filter(
            school=membership.school
        ).order_by("-submitted_at", "-id")
        stage = request.query_params.get("stage")
        if stage:
            applications = applications.filter(stage=stage)
        return Response(
            {"applications": [serialize_admission(item) for item in applications]}
        )


class SchoolRosterSummaryView(APIView):
    def get(self, request, school_id):
        membership = _membership(request, school_id, roles=_ROSTER_READ_ROLES)
        return Response(serialize_roster_summary(membership.school))
