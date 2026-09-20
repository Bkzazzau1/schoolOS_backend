from django.urls import path

from .views import ApproveProposalView, OnboardingView, RejectProposalView

urlpatterns = [
    path("me/onboarding/", OnboardingView.as_view()),
    path("schools/<uuid:school_id>/proposals/<str:proposal_id>/approve/", ApproveProposalView.as_view()),
    path("schools/<uuid:school_id>/proposals/<str:proposal_id>/reject/", RejectProposalView.as_view()),
]
