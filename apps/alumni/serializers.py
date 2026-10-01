from datetime import date

from rest_framework import serializers

from .models import (
    AlumniEvent,
    AlumniMentorProfile,
    AlumniMentorshipRequest,
    AlumniMentorshipRequestStatus,
    AlumniOpportunity,
    AlumniOpportunityType,
    AlumniPledge,
    AlumniPledgeCategory,
    AlumniPledgeStatus,
    AlumniProfile,
)


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


class AlumniDirectoryEntrySerializer(serializers.ModelSerializer):
    """A deliberately narrow, public-facing subset of an alumnus's profile - never the private
    fields (admission number, original student reference, email) `AlumniProfileSerializer` carries
    for the person's own self-view."""

    membershipId = serializers.UUIDField(source="membership_id", read_only=True)
    name = serializers.SerializerMethodField()
    graduationYear = serializers.IntegerField(source="graduation_year", read_only=True)
    graduationSet = serializers.CharField(source="graduation_set", read_only=True)
    locationText = serializers.CharField(source="location_text", read_only=True)

    class Meta:
        model = AlumniProfile
        fields = [
            "membershipId",
            "name",
            "graduationYear",
            "graduationSet",
            "profession",
            "organisation",
            "locationText",
            "bio",
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


class AlumniEventSerializer(serializers.ModelSerializer):
    """`attendingCount` and `myRsvp` are both real, computed from `AlumniEventRsvp` rows - never
    stored on the event itself. `myRsvp` needs the acting membership passed in as `context["viewer"]`."""

    id = serializers.UUIDField(read_only=True)
    timeText = serializers.CharField(source="time_text", read_only=True)
    attendingCount = serializers.SerializerMethodField()
    myRsvp = serializers.SerializerMethodField()

    class Meta:
        model = AlumniEvent
        fields = ["id", "title", "date", "timeText", "venue", "note", "attendingCount", "myRsvp"]
        read_only_fields = fields

    def get_attendingCount(self, obj):
        return obj.rsvps.filter(attending=True).count()

    def get_myRsvp(self, obj):
        viewer = self.context.get("viewer")
        if viewer is None:
            return None
        rsvp = obj.rsvps.filter(membership=viewer).first()
        return rsvp.attending if rsvp else None


class AlumniEventCreateSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=200)
    date = serializers.DateField()
    timeText = serializers.CharField(max_length=40, required=False, allow_blank=True)
    venue = serializers.CharField(max_length=200, required=False, allow_blank=True)
    note = serializers.CharField(max_length=2000, required=False, allow_blank=True)


class AlumniEventRsvpSerializer(serializers.Serializer):
    attending = serializers.BooleanField()


class AlumniPledgeSerializer(serializers.ModelSerializer):
    """One real, non-monetary offer of help. `alumniName` is included even on the alumnus's own
    self-view - no privacy concern in reflecting back your own name - so the same serializer
    serves both the self-service list and Alumni Management's oversight bundle."""

    id = serializers.UUIDField(read_only=True)
    alumniMembershipId = serializers.UUIDField(source="membership_id", read_only=True)
    alumniName = serializers.SerializerMethodField()
    schoolNote = serializers.CharField(source="school_note", read_only=True)
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)
    updatedAt = serializers.DateTimeField(source="updated_at", read_only=True)

    class Meta:
        model = AlumniPledge
        fields = [
            "id",
            "alumniMembershipId",
            "alumniName",
            "category",
            "description",
            "status",
            "schoolNote",
            "createdAt",
            "updatedAt",
        ]
        read_only_fields = fields

    def get_alumniName(self, obj):
        return obj.membership.user.get_full_name() or obj.membership.user.email


class AlumniPledgeCreateSerializer(serializers.Serializer):
    category = serializers.ChoiceField(choices=AlumniPledgeCategory.choices)
    description = serializers.CharField(max_length=2000, min_length=5)


class AlumniPledgeStatusSerializer(serializers.Serializer):
    status = serializers.ChoiceField(
        choices=[AlumniPledgeStatus.ACKNOWLEDGED, AlumniPledgeStatus.FULFILLED]
    )
    schoolNote = serializers.CharField(max_length=500, required=False, allow_blank=True)


class AlumniOpportunitySerializer(serializers.ModelSerializer):
    """A real posting board entry. `postedByName` is included for every viewer - the whole point
    is other alumni know who posted it - so the same serializer serves both the general list and
    Alumni Management's oversight bundle."""

    id = serializers.UUIDField(read_only=True)
    postedByMembershipId = serializers.UUIDField(source="posted_by_id", read_only=True)
    postedByName = serializers.SerializerMethodField()
    opportunityType = serializers.CharField(source="opportunity_type", read_only=True)
    locationText = serializers.CharField(source="location_text", read_only=True)
    contactInfo = serializers.CharField(source="contact_info", read_only=True)
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)

    class Meta:
        model = AlumniOpportunity
        fields = [
            "id",
            "postedByMembershipId",
            "postedByName",
            "title",
            "organisation",
            "opportunityType",
            "locationText",
            "description",
            "contactInfo",
            "status",
            "createdAt",
        ]
        read_only_fields = fields

    def get_postedByName(self, obj):
        return obj.posted_by.user.get_full_name() or obj.posted_by.user.email


class AlumniOpportunityCreateSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=200)
    organisation = serializers.CharField(max_length=200)
    opportunityType = serializers.ChoiceField(choices=AlumniOpportunityType.choices)
    locationText = serializers.CharField(max_length=160, required=False, allow_blank=True)
    description = serializers.CharField(max_length=4000, min_length=5)
    contactInfo = serializers.CharField(max_length=200, required=False, allow_blank=True)

    def validate(self, attrs):
        if not attrs.get("contactInfo", "").strip() and len(attrs["description"].strip()) < 20:
            raise serializers.ValidationError(
                "Add a contact method, or describe how an interested alumnus can follow up."
            )
        return attrs


class AlumniMentorProfileSerializer(serializers.ModelSerializer):
    """Never contact info - the same restraint `AlumniDirectoryEntrySerializer` already applies."""

    membershipId = serializers.UUIDField(source="membership_id", read_only=True)
    name = serializers.SerializerMethodField()
    isActive = serializers.BooleanField(source="is_active", read_only=True)
    updatedAt = serializers.DateTimeField(source="updated_at", read_only=True)

    class Meta:
        model = AlumniMentorProfile
        fields = ["membershipId", "name", "expertise", "bio", "isActive", "updatedAt"]
        read_only_fields = fields

    def get_name(self, obj):
        return obj.membership.user.get_full_name() or obj.membership.user.email


class AlumniMentorProfileWriteSerializer(serializers.Serializer):
    expertise = serializers.CharField(max_length=200, min_length=2)
    bio = serializers.CharField(max_length=2000, min_length=5)
    isActive = serializers.BooleanField(required=False, default=True)


class AlumniMentorshipRequestSerializer(serializers.ModelSerializer):
    """`mentorEmail`/`menteeEmail` are only ever present once `status` is really `accepted` - a
    pending or declined request never leaks either side's real contact info."""

    id = serializers.UUIDField(read_only=True)
    mentorMembershipId = serializers.UUIDField(source="mentor_id", read_only=True)
    mentorName = serializers.SerializerMethodField()
    mentorEmail = serializers.SerializerMethodField()
    menteeMembershipId = serializers.UUIDField(source="mentee_id", read_only=True)
    menteeName = serializers.SerializerMethodField()
    menteeEmail = serializers.SerializerMethodField()
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)

    class Meta:
        model = AlumniMentorshipRequest
        fields = [
            "id",
            "mentorMembershipId",
            "mentorName",
            "mentorEmail",
            "menteeMembershipId",
            "menteeName",
            "menteeEmail",
            "message",
            "status",
            "createdAt",
        ]
        read_only_fields = fields

    def get_mentorName(self, obj):
        return obj.mentor.user.get_full_name() or obj.mentor.user.email

    def get_menteeName(self, obj):
        return obj.mentee.user.get_full_name() or obj.mentee.user.email

    def get_mentorEmail(self, obj):
        return obj.mentor.user.email if obj.status == AlumniMentorshipRequestStatus.ACCEPTED else None

    def get_menteeEmail(self, obj):
        return obj.mentee.user.email if obj.status == AlumniMentorshipRequestStatus.ACCEPTED else None


class AlumniMentorshipRequestCreateSerializer(serializers.Serializer):
    mentorMembershipId = serializers.UUIDField()
    message = serializers.CharField(max_length=2000, required=False, allow_blank=True)


class AlumniMentorshipRequestRespondSerializer(serializers.Serializer):
    status = serializers.ChoiceField(
        choices=[AlumniMentorshipRequestStatus.ACCEPTED, AlumniMentorshipRequestStatus.DECLINED]
    )
