import json

from django.conf import settings
from rest_framework import serializers


class MutationSerializer(serializers.Serializer):
    """One queued mutation, in the camelCase shape the app already uses."""

    id = serializers.CharField(max_length=64)
    tenantId = serializers.UUIDField()
    membershipId = serializers.UUIDField()
    entityType = serializers.RegexField(r"^[a-z][a-z0-9_]{0,63}$")
    entityId = serializers.RegexField(r"^[A-Za-z0-9._:\-]{1,128}$")
    operation = serializers.ChoiceField(choices=["create", "update", "delete"])
    payload = serializers.DictField(required=False)
    baseVersion = serializers.IntegerField(required=False, allow_null=True, min_value=0)
    createdAt = serializers.DateTimeField(required=False)

    def validate_payload(self, value):
        if len(json.dumps(value)) > settings.SYNC_MAX_PAYLOAD_BYTES:
            raise serializers.ValidationError("Payload is too large.")
        return value

    def validate(self, attrs):
        if attrs["operation"] in ("create", "update") and not attrs.get("payload"):
            raise serializers.ValidationError({"payload": "A payload is required."})
        return attrs
