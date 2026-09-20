from rest_framework import serializers

from .models import MembershipActivity
from .services import AFTER_SYNC, MODES


class OverrideSerializer(serializers.Serializer):
    effect = serializers.ChoiceField(choices=MembershipActivity.Effect.values)
    expiresAt = serializers.DateTimeField(required=False, allow_null=True)
    note = serializers.CharField(required=False, allow_blank=True, max_length=200)
    #: For a block: wait for the person's next sync (default) or take effect now.
    mode = serializers.ChoiceField(choices=MODES, required=False, default=AFTER_SYNC)


class RoleDefaultsSerializer(serializers.Serializer):
    activities = serializers.ListField(child=serializers.CharField(max_length=64), allow_empty=True)


class ReassignSerializer(serializers.Serializer):
    activity = serializers.CharField(max_length=64)
    fromMembershipId = serializers.UUIDField()
    toMembershipId = serializers.UUIDField()
    note = serializers.CharField(required=False, allow_blank=True, max_length=200)
    mode = serializers.ChoiceField(choices=MODES, required=False, default=AFTER_SYNC)


class AcknowledgeSerializer(serializers.Serializer):
    activities = serializers.ListField(child=serializers.CharField(max_length=64), allow_empty=False, max_length=200)
