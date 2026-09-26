from django.utils import timezone
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.permissions import membership_from_request
from rest_framework.exceptions import PermissionDenied

from . import attention, bank_collections, concessions, payroll, staff, structure

#: What the app's dashboards show that the server cannot work out yet, because the
#: records do not exist here. The app must not fill these in with made-up numbers.
NOT_AVAILABLE = ["students", "attendance", "academic results", "campus comparison"]

#: Money received is real once a bank account is connected; until then there is nothing to show.
FEE_COLLECTION = "fee collection"


def _member(request, school_id, roles):
    member = membership_from_request(request, school_id)
    if member.role not in roles:
        raise PermissionDenied("You do not have access to this dashboard.")
    return member


class OwnerDashboardView(APIView):
    """GET dashboards/schools/<school>/owner/  (owner only)

    What the school's own records say: staff, payroll, scholarships and discounts,
    sections, and what needs the owner's attention.
    """

    def get(self, request, school_id):
        school = _member(request, school_id, {"proprietor"}).school
        money = bank_collections.summary(school)
        return Response({
            "generatedAt": timezone.now().isoformat(),
            "attention": attention.build(school),
            "staff": staff.summary(school),
            "payroll": payroll.summary(school),
            "concessions": concessions.summary(school),
            "structure": structure.summary(school),
            "collections": money,
            "notAvailableYet": NOT_AVAILABLE if money["available"] else [*NOT_AVAILABLE, FEE_COLLECTION],
        })


class FinanceDashboardView(APIView):
    """GET dashboards/schools/<school>/finance/  (owner and finance officers)"""

    def get(self, request, school_id):
        school = _member(request, school_id, {"proprietor", "accountant"}).school
        money = bank_collections.summary(school)
        return Response({
            "generatedAt": timezone.now().isoformat(),
            "payroll": payroll.summary(school),
            "monthlyPayroll": staff.summary(school)["monthlyPayroll"],
            "concessions": concessions.summary(school),
            "collections": money,
            "notAvailableYet": [*([] if money["available"] else [FEE_COLLECTION]), "outstanding balances", "family accounts"],
        })
