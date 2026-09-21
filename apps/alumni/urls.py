from django.urls import path

from .views import MyAlumniProfileView


urlpatterns = [
    path(
        "schools/<uuid:school_id>/me/",
        MyAlumniProfileView.as_view(),
        name="alumni-me",
    ),
]
