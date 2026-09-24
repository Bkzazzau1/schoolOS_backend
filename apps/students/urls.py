from django.urls import path

from .views import (
    SchoolAdmissionsListView,
    SchoolRosterSummaryView,
    SchoolStudentDetailView,
    SchoolStudentListView,
)

urlpatterns = [
    path("schools/<uuid:school_id>/students/", SchoolStudentListView.as_view()),
    path(
        "schools/<uuid:school_id>/students/<uuid:student_id>/",
        SchoolStudentDetailView.as_view(),
    ),
    path("schools/<uuid:school_id>/admissions/", SchoolAdmissionsListView.as_view()),
    path("schools/<uuid:school_id>/roster/", SchoolRosterSummaryView.as_view()),
]
