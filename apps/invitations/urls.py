from django.urls import path

from .views_owner import StaffInvitationView, UnlinkView
from .views_public import AcceptView, PreviewView

urlpatterns = [
    path("invitations/<str:token>/", PreviewView.as_view()),
    path("invitations/<str:token>/accept/", AcceptView.as_view()),
    path("owner/schools/<uuid:school_id>/staff/<str:staff_id>/invitation/", StaffInvitationView.as_view()),
    path("owner/schools/<uuid:school_id>/staff/<str:staff_id>/unlink/", UnlinkView.as_view()),
]
