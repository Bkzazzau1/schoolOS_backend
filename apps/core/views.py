from django.http import JsonResponse
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny


@api_view(["GET"])
@permission_classes([AllowAny])
@throttle_classes([])
def health(_request):
    return JsonResponse({"status": "ok"})
