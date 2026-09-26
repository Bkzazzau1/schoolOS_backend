"""Mandates: staff start and watch them; the payer authorises and activates their own. Nothing here ever returns an account number."""

from datetime import date

from django.utils.decorators import method_decorator
from django.views.decorators.debug import sensitive_post_parameters
from rest_framework import status
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.permissions import require_membership
from apps.receivables.models import Family, FamilyGuardian
from apps.schools.models import Role

from . import mandate_provider, mandates, serializers
from .constants import LIVE_MANDATE, MandateStatus
from .errors import MandateRefused
from .http import body, is_uuid, mandate_errors
from .models import DirectDebitMandate
from .permissions import NEED_MANAGE, NEED_VIEW, acting_membership, permissions_of

_SENSITIVE = method_decorator(sensitive_post_parameters("accountNumber", "account_number", "answers", "credentials"), name="dispatch")


def _date(value, name: str):
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise MandateRefused(f"'{name}' is not a date (use YYYY-MM-DD).", "invalid_dates")


@_SENSITIVE
class MandatesView(APIView):
    def get(self, request, school_id):
        membership = acting_membership(request, school_id, need=NEED_VIEW)
        q = request.query_params
        family = None
        if q.get("family"):
            if not is_uuid(q["family"]):
                return Response({"code": "invalid_filter", "message": "'family' is not valid."}, status=status.HTTP_400_BAD_REQUEST)
            family = Family.objects.filter(school=membership.school, id=q["family"]).first()
            if family is None:
                raise NotFound("That family was not found.")
        ready = {"1": True, "0": False}.get(q.get("debitReady", ""))
        rows = mandates.list_mandates(membership, family=family, status=q.get("status", ""), provider=q.get("provider", ""), debit_ready=ready, search=q.get("q", "").strip())
        everything = DirectDebitMandate.objects.filter(school=membership.school)
        by_status = {s: everything.filter(status=s).count() for s in MandateStatus.values}
        return Response({"mandates": [serializers.mandate(m) for m in rows], "counts": by_status, "permissions": permissions_of(membership)})

    @mandate_errors
    def post(self, request, school_id):
        """Start a mandate for a payer. Staff can never authorise it for them: the payer does that themselves."""
        membership = acting_membership(request, school_id, need=NEED_MANAGE)
        d = body(request)
        mandate = mandates.start(
            membership, family_id=d.get("familyId"), payer_id=d.get("payerId"), connection_id=d.get("connectionId"), bank_code=d.get("bankCode"),
            account_number=d.get("accountNumber"), maximum_amount_minor=d.get("maximumAmountMinor"), start_date=_date(d.get("startDate"), "startDate"),
            end_date=_date(d.get("endDate"), "endDate"), max_debits=d.get("maxDebits"), consent_route=d.get("consentRoute") or "",
            provider_customer_ref=d.get("providerCustomerRef") or "",
        )
        mandate = mandates.get_mandate(membership, mandate.id)
        return Response({"mandate": serializers.mandate(mandate, detail=True)}, status=status.HTTP_201_CREATED)


class FamilyPayersView(APIView):
    """GET a family's payers (its guardians), so a mandate can be started for one of them."""

    def get(self, request, school_id, family_id):
        membership = acting_membership(request, school_id, need=NEED_VIEW)
        family = Family.objects.filter(school=membership.school, id=family_id).first()
        if family is None:
            raise NotFound("That family was not found.")
        rows = FamilyGuardian.objects.select_related("guardian").filter(family=family, is_active=True)
        return Response({"payers": [
            {"id": str(p.id), "name": p.guardian.name, "relationship": p.guardian.relationship, "isPrimaryPayer": p.is_primary_payer,
             "hasAppAccount": bool(p.guardian.account_user_id), "hasEmail": bool(p.guardian.email), "hasPhone": bool(p.guardian.phone)}
            for p in rows
        ]})


class MandateDetailView(APIView):
    def get(self, request, school_id, mandate_id):
        membership = acting_membership(request, school_id, need=NEED_VIEW)
        mandate = mandates.get_mandate(membership, mandate_id)
        return Response({"mandate": serializers.mandate(mandate, detail=True), "permissions": permissions_of(membership)})


def _one(result):
    return {"mandate": serializers.mandate(result, detail=True)}


ACTIONS = {
    "refresh": lambda m, i, d: _one(mandate_provider.refresh(m, i)),
    "retry-setup": lambda m, i, d: _one(mandate_provider.retry_setup(m, i)),
    "resend-activation": lambda m, i, d: _one(mandate_provider.resend_activation(m, i)),
    "cancel": lambda m, i, d: _one(mandate_provider.cancel(m, i, reason=d.get("reason") or "")),
    "suspend": lambda m, i, d: _one(mandate_provider.suspend(m, i)),
    "reactivate": lambda m, i, d: _one(mandate_provider.reactivate(m, i)),
    "primary": lambda m, i, d: _one(mandates.set_primary(m, i)),
}


class MandateActionView(APIView):
    action = None

    @mandate_errors
    def post(self, request, school_id, mandate_id):
        membership = acting_membership(request, school_id, need=NEED_VIEW)
        return Response(ACTIONS[self.action](membership, mandate_id, body(request)))


# -- the payer ----------------------------------------------------------------------------------------


def _parent(request, school_id):
    query = getattr(request, "query_params", {})
    return require_membership(request.user, school_id, roles=[Role.PARENT], membership_id=query.get("membership") or None)


class PayerMandatesView(APIView):
    """The signed-in payer's OWN mandates: found through the guardian record linked to their account, never through anything the app claims."""

    def get(self, request, school_id):
        membership = _parent(request, school_id)
        rows = (
            DirectDebitMandate.objects.select_related("family", "payer__guardian", "provider_connection", "school")
            .filter(school=membership.school, payer__guardian__account_user_id=membership.user_id).order_by("-created_at")
        )
        live = [m for m in rows if m.status in LIVE_MANDATE]
        return Response({"mandates": [serializers.payer_view(m) for m in rows], "waiting": sum(1 for m in live if m.status in (MandateStatus.PENDING_CONSENT, MandateStatus.PENDING_ACTIVATION))})


class PayerMandateDetailView(APIView):
    def get(self, request, school_id, mandate_id):
        membership = _parent(request, school_id)
        return Response({"mandate": serializers.payer_view(mandate_provider.payer_mandate(membership, mandate_id))})


def _payer(result):
    return {"mandate": serializers.payer_view(result)}


PAYER_ACTIONS = {
    "consent": lambda m, i, d: _payer(mandate_provider.payer_consent(m, i, shown_hash=str(d.get("consentTextHash") or ""), accepted=d.get("accepted"))),
    "activation-request": lambda m, i, d: mandate_provider.request_activation(m, i),
    "activation-confirm": lambda m, i, d: _payer(mandate_provider.confirm_activation(m, i, answers=d.get("answers"))),
    "refresh": lambda m, i, d: _payer(mandate_provider.refresh_mandate(mandate_provider.payer_mandate(m, i).id, actor=m)),
    "cancel": lambda m, i, d: _payer(mandate_provider.payer_cancel(m, i, reason=d.get("reason") or "")),
}


@_SENSITIVE
class PayerMandateActionView(APIView):
    action = None

    @mandate_errors
    def post(self, request, school_id, mandate_id):
        membership = _parent(request, school_id)
        return Response(PAYER_ACTIONS[self.action](membership, mandate_id, body(request)))
