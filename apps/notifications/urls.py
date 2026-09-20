from django.urls import path

from .views import InboxView, ReadAllView, ReadOneView

urlpatterns = [
    path("schools/<uuid:school_id>/notifications/", InboxView.as_view()),
    path("schools/<uuid:school_id>/notifications/read-all/", ReadAllView.as_view()),
    path("schools/<uuid:school_id>/notifications/<int:notification_id>/read/", ReadOneView.as_view()),
]
