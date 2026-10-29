from django.urls import path

from . import views

app_name = "console"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("organizations/", views.organization_list, name="organizations"),
    path("organizations/<uuid:organization_id>/", views.organization_detail, name="organization_detail"),
    path("pricing/", views.pricing_overview, name="pricing"),
    path("schools/<uuid:school_id>/enterprise/", views.school_enterprise_agreement, name="school_enterprise_agreement"),
]
