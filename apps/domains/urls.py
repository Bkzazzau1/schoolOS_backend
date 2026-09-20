from django.urls import path

from .views import assetlinks

urlpatterns = [
    path(".well-known/assetlinks.json", assetlinks),
]
