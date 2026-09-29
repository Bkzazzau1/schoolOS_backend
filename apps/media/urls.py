from django.urls import path

from . import views

_BASE = "schools/<uuid:school_id>/media/"

urlpatterns = [
    path(f"{_BASE}assets/", views.AssetsView.as_view()),
    path(f"{_BASE}assets/<uuid:asset_id>/", views.AssetDetailView.as_view()),
    path(f"{_BASE}assets/<uuid:asset_id>/upload/", views.UploadBodyView.as_view()),
    path(f"{_BASE}assets/<uuid:asset_id>/complete/", views.CompleteView.as_view()),
    path(f"{_BASE}assets/<uuid:asset_id>/download/", views.DownloadInfoView.as_view()),
    path(f"{_BASE}assets/<uuid:asset_id>/raw/", views.RawView.as_view()),
    path(f"{_BASE}assets/<uuid:asset_id>/retire/", views.RetireView.as_view()),
    path(f"{_BASE}assets/<uuid:asset_id>/audit/", views.AuditView.as_view()),
]
