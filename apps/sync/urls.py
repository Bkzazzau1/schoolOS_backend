from django.urls import path

from .views import PushView

urlpatterns = [
    path("push/", PushView.as_view()),
]
