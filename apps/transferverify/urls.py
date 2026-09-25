from django.urls import path

from .views import (
    ApproveAssociationMemberView,
    AssociationCatalogView,
    AssociationMembersView,
    ExitAssociationMembershipView,
    JoinAssociationView,
    NetworkPhoneMatchView,
    RejectAssociationMemberView,
    SchoolAssociationMembershipsView,
    SuspendAssociationMemberView,
)

urlpatterns = [
    path("transferverify/associations/", AssociationCatalogView.as_view()),
    path(
        "schools/<uuid:school_id>/transferverify/associations/",
        SchoolAssociationMembershipsView.as_view(),
    ),
    path(
        "schools/<uuid:school_id>/transferverify/associations/<uuid:association_id>/join/",
        JoinAssociationView.as_view(),
    ),
    path(
        "schools/<uuid:school_id>/transferverify/associations/memberships/<uuid:membership_id>/exit/",
        ExitAssociationMembershipView.as_view(),
    ),
    path(
        "transferverify/associations/<uuid:association_id>/members/",
        AssociationMembersView.as_view(),
    ),
    path(
        "transferverify/associations/<uuid:association_id>/members/<uuid:membership_id>/approve/",
        ApproveAssociationMemberView.as_view(),
    ),
    path(
        "transferverify/associations/<uuid:association_id>/members/<uuid:membership_id>/reject/",
        RejectAssociationMemberView.as_view(),
    ),
    path(
        "transferverify/associations/<uuid:association_id>/members/<uuid:membership_id>/suspend/",
        SuspendAssociationMemberView.as_view(),
    ),
    path(
        "schools/<uuid:school_id>/transferverify/network/match/",
        NetworkPhoneMatchView.as_view(),
    ),
]
