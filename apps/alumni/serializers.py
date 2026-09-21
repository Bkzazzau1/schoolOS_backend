from rest_framework import serializers

from .models import AlumniProfile


class AlumniProfileSerializer(serializers.ModelSerializer):
    membershipId = serializers.UUIDField(source="membership_id", read_only=True)
    schoolId = serializers.UUIDField(source="school_id", read_only=True)
    email = serializers.EmailField(source="membership.user.email", read_only=True)

    class Meta:
        model = AlumniProfile
        fields = [
            "membershipId",
            "schoolId",
            "email",
            "original_student_reference",
            "admission_number",
            "graduation_year",
            "graduation_set",
            "verification_status",
            "profession",
            "organisation",
            "location_text",
            "bio",
            "directory_visible",
            "verified_at",
            "updated_at",
        ]
        read_only_fields = fields
