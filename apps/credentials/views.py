from rest_framework import status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.permissions import require_membership
from apps.schools.models import Role
from apps.students.models import Student

from .models import CredentialRecoveryRequest, RecoveryStatus
from .services import (
    CredentialManagementError,
    change_parent_phone,
    credential_handoff,
    dismiss_recovery,
    request_recovery,
    reset_parent_credentials,
    reset_student_credentials,
    serialize_recovery_request,
)
from .throttles import RecoveryIdentifierThrottle, RecoveryNetworkThrottle


_MANAGER_ROLES = {Role.PROPRIETOR, Role.ADMINISTRATOR}


def _manager(request, school_id):
    return require_membership(
        request.user,
        school_id,
        roles=_MANAGER_ROLES,
        membership_id=request.query_params.get("membership"),
    )


def _student_for(manager, student_id):
    try:
        return Student.objects.select_related("account_user").get(
            id=student_id,
            school=manager.school,
        )
    except Student.DoesNotExist as exc:
        raise NotFound("That student is not available to this school.") from exc


def _run(action):
    try:
        return action()
    except CredentialManagementError as exc:
        raise ValidationError({"message": exc.message}) from exc


class PublicRecoveryRequestView(APIView):
    """Accept a forgotten-password request without account enumeration."""

    authentication_classes = ()
    permission_classes = (AllowAny,)
    throttle_classes = (RecoveryIdentifierThrottle, RecoveryNetworkThrottle)

    def post(self, request):
        request_recovery(request.data.get("identifier"))
        return Response(
            {
                "accepted": True,
                "message": (
                    "If that login ID belongs to a SchoolOS Student or Parent account, "
                    "the school office can now review the recovery request."
                ),
            },
            status=status.HTTP_202_ACCEPTED,
        )


class SchoolRecoveryRequestListView(APIView):
    def get(self, request, school_id):
        manager = _manager(request, school_id)
        requested_status = request.query_params.get("status", RecoveryStatus.PENDING)
        if requested_status not in RecoveryStatus.values:
            raise ValidationError({"status": "Unknown recovery status."})
        items = CredentialRecoveryRequest.objects.select_related("user").filter(
            school=manager.school,
            status=requested_status,
        )
        return Response(
            {"requests": [serialize_recovery_request(item) for item in items]}
        )


class SchoolRecoveryRequestDismissView(APIView):
    def post(self, request, school_id, request_id):
        manager = _manager(request, school_id)
        try:
            item = CredentialRecoveryRequest.objects.get(
                id=request_id,
                school=manager.school,
            )
        except CredentialRecoveryRequest.DoesNotExist as exc:
            raise NotFound("That recovery request is not available to this school.") from exc
        dismiss_recovery(recovery=item, actor=manager)
        return Response({"dismissed": True})


class StudentCredentialHandoffView(APIView):
    def get(self, request, school_id, student_id):
        manager = _manager(request, school_id)
        student = _student_for(manager, student_id)
        return Response(_run(lambda: credential_handoff(student)))


class StudentCredentialResetView(APIView):
    def post(self, request, school_id, student_id):
        manager = _manager(request, school_id)
        student = _student_for(manager, student_id)
        return Response(
            _run(lambda: reset_student_credentials(student=student, actor=manager))
        )


class ParentCredentialResetView(APIView):
    def post(self, request, school_id, student_id):
        manager = _manager(request, school_id)
        student = _student_for(manager, student_id)
        return Response(
            _run(lambda: reset_parent_credentials(student=student, actor=manager))
        )


class ParentPhoneChangeView(APIView):
    def post(self, request, school_id, student_id):
        manager = _manager(request, school_id)
        student = _student_for(manager, student_id)
        return Response(
            _run(
                lambda: change_parent_phone(
                    student=student,
                    actor=manager,
                    new_phone=request.data.get("phone"),
                )
            )
        )
