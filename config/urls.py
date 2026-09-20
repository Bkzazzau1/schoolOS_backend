from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView


@api_view(["GET"])
@permission_classes([AllowAny])
@throttle_classes([])
def health(_request):
    return JsonResponse({"status": "ok"})


api_v1 = [
    path("health/", health),
    path("auth/token/", TokenObtainPairView.as_view()),
    path("auth/token/refresh/", TokenRefreshView.as_view()),
    path("", include("apps.schools.urls")),
    path("sync/", include("apps.sync.urls")),
]

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", include(api_v1)),
]
