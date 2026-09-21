from datetime import date

from rest_framework import serializers

from .models import AlumniProfile


class AlumniProfileSerializer(serializers.ModelSerializer):
    membershipId = serializers.UUIDField(source="membership_id", read_only=True)
    schoolId = serializers.UUIDField(source="school_id", read_only=True)
    email = serializers.EmailField(source="membership.user.email", read_only=True)
    name = serializers.SerializerMethodField()
    verifiedByMembershipId = serializers.UUIDField(source="verified_by_id", read_only=True)

    class Meta:
        model = AlumniProfile
        fields = [
            "membershipId",
            "schoolId",
            "email",
            "name",
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
            "submitted_at",
            "reviewed_at",
            "verification_note",
            "verified_at",
            "verifiedByMembershipId",
            "updated_at",
        ]
        read_only_fields = fields

    def get_name(self, obj):
        return obj.membership.user.get_full_name() or obj.membership.user.email


class AlumniSelfProfileWriteSerializer(serializers.Serializer):
    original_student_reference = serializers.CharField(max_length=120, required=False, allow_blank=True)
    admission_number = serializers.CharField(max_length=80, required=False, allow_blank=True)
    graduation_year = serializers.IntegerField(required=False, allow_null=True)
    graduation_set = serializers.CharField(max_length=120, required=False, allow_blank=True)
    profession = serializers.CharField(max_length=160, required=False, allow_blank=True)
    organisation = serializers.CharField(max_length=200, required=False, allow_blank=True)
    location_text = serializers.CharField(max_length=160, required=False, allow_blank=True)
    bio = serializers.CharField(max_length=2000, required=False, allow_blank=True)
    directory_visible = serializers.BooleanField(required=False)

    def validate_graduation_year(self, value):
        if value is None:
            return value
        if value < 1900 or value > date.today().year + 1:
            raise serializers.ValidationError("Enter a valid graduation year.")
        return value


class AlumniTransitionSerializer(serializers.Serializer):
    studentMembershipId = serializers.UUIDField()
    originalStudentReference = serializers.CharField(max_length=120, required=False, allow_blank=True)
    admissionNumber = serializers.CharField(max_length=80, required=False, allow_blank=True)
    graduationYear = serializers.IntegerField()
    graduationSet = serializers.CharField(max_length=120, required=False, allow_blank=True)

    def validate_graduationYear(self, value):
        if value < 1900 or value > date.today().year + 1:
            raise serializers.ValidationError("Enter a valid graduation year.")
        return value


class AlumniReviewSerializer(serializers.Serializer):
    note = serializers.CharField(max_length=500, required=False, allow_blank=True)


class AlumniRejectSerializer(serializers.Serializer):
    note = serializers.CharField(max_length=500, allow_blank=False)
