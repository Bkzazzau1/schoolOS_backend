from django.urls import path

from .views_member import AcknowledgeView, MyAccessView
from .views_owner import (
    AuditView,
    CatalogView,
    PeopleView,
    PersonActivityView,
    ReassignView,
    RoleDetailView,
    RolesView,
)

member_urls = [
    path("schools/<uuid:school_id>/access/me/", MyAccessView.as_view()),
    path("schools/<uuid:school_id>/access/acknowledge/", AcknowledgeView.as_view()),
]

owner_urls = [
    path("owner/schools/<uuid:school_id>/access/catalog/", CatalogView.as_view()),
    path("owner/schools/<uuid:school_id>/access/roles/", RolesView.as_view()),
    path("owner/schools/<uuid:school_id>/access/roles/<slug:role>/", RoleDetailView.as_view()),
    path("owner/schools/<uuid:school_id>/access/people/", PeopleView.as_view()),
    path(
        "owner/schools/<uuid:school_id>/access/people/<uuid:membership_id>/activities/<str:activity>/",
        PersonActivityView.as_view(),
    ),
    path("owner/schools/<uuid:school_id>/access/reassign/", ReassignView.as_view()),
    path("owner/schools/<uuid:school_id>/access/audit/", AuditView.as_view()),
]

urlpatterns = member_urls + owner_urls
