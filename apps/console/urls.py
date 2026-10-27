from django.urls import path

from . import views

app_name = "console"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("organizations/", views.organization_list, name="organizations"),
    path("organizations/<uuid:organization_id>/", views.organization_detail, name="organization_detail"),
]
