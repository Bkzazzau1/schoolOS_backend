"""Every /api/v1/ route, one line per feature.

A feature owns its own urls.py. To add a feature, add its line here.
"""

from django.urls import include, path

urlpatterns = [
    path("", include("apps.core.urls")),
    path("auth/", include("apps.accounts.urls")),
    path("", include("apps.schools.urls")),
    path("", include("apps.notifications.urls")),
    path("", include("apps.access.urls")),
    path("sync/", include("apps.sync.urls")),
    path("owner/", include("apps.owner.urls")),
    path("staff/", include("apps.staff.urls")),
    path("alumni/", include("apps.alumni.urls")),
    path("", include("apps.invitations.urls")),
    path("dashboards/", include("apps.dashboards.urls")),
]
