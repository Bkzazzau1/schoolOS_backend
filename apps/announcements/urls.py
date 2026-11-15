from django.urls import path

from .views import ActiveAnnouncementsView

urlpatterns = [
    path("schools/<uuid:school_id>/announcements/active/", ActiveAnnouncementsView.as_view()),
]
