from django.utils import timezone
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.permissions import membership_from_request
from rest_framework.exceptions import PermissionDenied

from . import attention, concessions, payroll, staff, structure

#: What the app's dashboards show that the server cannot work out yet, because the
#: records do not exist here. The app must not fill these in with made-up numbers.
NOT_AVAILABLE = ["students", "attendance", "academic results", "fee collection", "campus comparison"]


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
        return Response({
            "generatedAt": timezone.now().isoformat(),
            "attention": attention.build(school),
            "staff": staff.summary(school),
            "payroll": payroll.summary(school),
            "concessions": concessions.summary(school),
            "structure": structure.summary(school),
            "notAvailableYet": NOT_AVAILABLE,
        })


class FinanceDashboardView(APIView):
    """GET dashboards/schools/<school>/finance/  (owner and finance officers)"""

    def get(self, request, school_id):
        school = _member(request, school_id, {"proprietor", "accountant"}).school
        return Response({
            "generatedAt": timezone.now().isoformat(),
            "payroll": payroll.summary(school),
            "monthlyPayroll": staff.summary(school)["monthlyPayroll"],
            "concessions": concessions.summary(school),
            "notAvailableYet": ["fee collection", "outstanding balances", "family accounts"],
        })
