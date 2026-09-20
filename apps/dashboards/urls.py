from django.urls import path

from .views import FinanceDashboardView, OwnerDashboardView

urlpatterns = [
    path("schools/<uuid:school_id>/owner/", OwnerDashboardView.as_view()),
    path("schools/<uuid:school_id>/finance/", FinanceDashboardView.as_view()),
]
