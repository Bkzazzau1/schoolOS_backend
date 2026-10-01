from django.urls import path

from .views import (
    AlumniDirectoryView,
    AlumniEventListView,
    AlumniEventRsvpView,
    AlumniManagementView,
    AlumniRejectView,
    AlumniTransitionView,
    AlumniVerifyView,
    MyAlumniProfileView,
)


urlpatterns = [
    path(
        "schools/<uuid:school_id>/me/",
        MyAlumniProfileView.as_view(),
        name="alumni-me",
    ),
    path(
        "schools/<uuid:school_id>/directory/",
        AlumniDirectoryView.as_view(),
        name="alumni-directory",
    ),
    path(
        "schools/<uuid:school_id>/events/",
        AlumniEventListView.as_view(),
        name="alumni-events",
    ),
    path(
        "schools/<uuid:school_id>/events/<uuid:event_id>/rsvp/",
        AlumniEventRsvpView.as_view(),
        name="alumni-event-rsvp",
    ),
    path(
        "schools/<uuid:school_id>/management/",
        AlumniManagementView.as_view(),
        name="alumni-management",
    ),
    path(
        "schools/<uuid:school_id>/management/transitions/",
        AlumniTransitionView.as_view(),
        name="alumni-transition",
    ),
    path(
        "schools/<uuid:school_id>/management/<uuid:alumni_membership_id>/verify/",
        AlumniVerifyView.as_view(),
        name="alumni-verify",
    ),
    path(
        "schools/<uuid:school_id>/management/<uuid:alumni_membership_id>/reject/",
        AlumniRejectView.as_view(),
        name="alumni-reject",
    ),
]
