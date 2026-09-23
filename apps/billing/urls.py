from django.urls import path

from .views import OrganizationSubscriptionView, PlanListView

urlpatterns = [
    path("plans/", PlanListView.as_view()),
    path(
        "organizations/<uuid:organization_id>/subscription/",
        OrganizationSubscriptionView.as_view(),
    ),
]
