from django.urls import path

from .views import RecordsView

urlpatterns = [
    path("schools/<uuid:school_id>/records/<slug:entity_type>/", RecordsView.as_view()),
]
