"""What a parent sees: their own family's statement and where to pay - nothing about any other family, and
nothing about how the school decides fees. A parent is found through the guardian records linked to their
signed-in account, never through anything the app claims."""

from rest_framework.exceptions import NotFound
from rest_framework.response import Response

from apps.core.permissions import require_membership
from apps.schools.models import Role
from apps.students.models import GuardianLink

from . import ledger, serializers, statements
from .http import ReceivablesView
from .models import Family


def _parent(request, school_id):
    query = getattr(request, "query_params", {})
    membership = require_membership(request.user, school_id, roles=[Role.PARENT], membership_id=query.get("membership") or None)
    return membership


def _my_families(membership) -> list:
    links = GuardianLink.objects.filter(account_user=membership.user, student__school=membership.school).values_list("student_id", flat=True)
    return list(Family.objects.filter(school=membership.school, members__student_id__in=set(links), members__is_active=True).distinct().order_by("display_name"))


class MyFamiliesView(ReceivablesView):
    """The families this parent's children belong to, with what is owed and where to pay."""

    def get(self, request, school_id):
        membership = _parent(request, school_id)
        found = _my_families(membership)
        return Response({
            "families": [
                {
                    **serializers.family(f), "position": serializers.position(ledger.family_position(f)),
                    "collectionAccounts": statements.collection_account_facts(f),
                }
                for f in found
            ]
        })


class MyFamilyStatementView(ReceivablesView):
    def get(self, request, school_id, family_id):
        membership = _parent(request, school_id)
        family = next((f for f in _my_families(membership) if f.id == family_id), None)
        if family is None:
            raise NotFound("That family was not found.")  # also what someone else's family looks like
        return Response({"statement": statements.build(family)})
