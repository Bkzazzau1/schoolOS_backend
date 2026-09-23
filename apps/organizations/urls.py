from django.urls import path

from .views import OrganizationListCreateView, OrganizationSchoolsView

urlpatterns = [
    path("organizations/", OrganizationListCreateView.as_view()),
    path(
        "organizations/<uuid:organization_id>/schools/",
        OrganizationSchoolsView.as_view(),
    ),
]
