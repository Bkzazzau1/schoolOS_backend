from django.urls import path

from .web import invite_page, registration_page

urlpatterns = [
    path("registration/", registration_page),
    path("<str:token>/", invite_page),
]
