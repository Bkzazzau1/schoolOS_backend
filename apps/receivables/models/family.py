import uuid

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.schools.models import Membership, School
from apps.students.models import GuardianLink, Student


class FamilyStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    #: Closed: no new charges. Its history stays.
    INACTIVE = "inactive", "Inactive"


class Family(models.Model):
    """The household a school bills. Students who are siblings share one family, so what they owe is
    added up in one place and paid into one account.

    This is the canonical financial relationship. The free-text `family_account_ref` and
    `sibling_link` on student registrations are only what someone typed at admission; they are never
    trusted to decide who owes what (see `bridge.py`).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="families")
    #: What the school and the family quote, like `FAM-K7Q2M9XA`. Fixed for life and unique at the school.
    code = models.CharField(max_length=32)
    display_name = models.CharField(max_length=200)
    status = models.CharField(max_length=12, choices=FamilyStatus.choices, default=FamilyStatus.ACTIVE)
    #: Where this family came from when it was made by the bridge from older records (empty when a
    #: person made it). Lets the bridge be run again without making a second family for the same reference.
    origin = models.CharField(max_length=200, blank=True)
    #: Set when two households were found to be one and this one was folded into the other (`merging.py`). A merged
    #: family is INACTIVE and keeps its code and history; everything it owed, paid and held now belongs to `merged_into`.
    merged_into = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="merged_families")
    merged_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(Membership, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["display_name", "code"]
        verbose_name_plural = "families"
        constraints = [
            models.UniqueConstraint(fields=["school", "code"], name="unique_family_code_per_school"),
            models.CheckConstraint(condition=~Q(merged_into=models.F("id")), name="family_not_merged_into_itself"),
            models.UniqueConstraint(
                fields=["school", "origin"], condition=~Q(origin=""), name="unique_family_origin_per_school"
            ),
        ]
        indexes = [models.Index(fields=["school", "status"])]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            stored = Family.objects.filter(pk=self.pk).values_list("code", "school_id").first()
            if stored is not None and stored != (self.code, self.school_id):
                raise ValidationError("A family's code and school never change.")
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.code} {self.display_name}"


class FamilyStudent(models.Model):
    """A student's membership of a family. A student is in at most one ACTIVE family (a database
    rule); leaving keeps the row with `left_at`, because charges raised while they were in it stay
    with that family's history."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="+")
    family = models.ForeignKey(Family, on_delete=models.PROTECT, related_name="members")
    student = models.ForeignKey(Student, on_delete=models.PROTECT, related_name="family_memberships")
    is_active = models.BooleanField(default=True)
    joined_at = models.DateTimeField(default=timezone.now)
    left_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(Membership, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        ordering = ["joined_at", "id"]
        constraints = [
            models.UniqueConstraint(fields=["student"], condition=Q(is_active=True), name="one_active_family_per_student"),
            models.CheckConstraint(
                condition=Q(is_active=True, left_at__isnull=True) | Q(is_active=False, left_at__isnull=False),
                name="family_student_left_matches_active",
            ),
        ]
        indexes = [models.Index(fields=["school", "family", "is_active"])]

    def save(self, *args, **kwargs):
        # One school throughout: a family and a student from different schools can never be linked.
        if self.family.school_id != self.student.school_id:
            raise ValidationError("A family and its students must belong to the same school.")
        self.school_id = self.family.school_id
        super().save(*args, **kwargs)


class FamilyGuardian(models.Model):
    """Who pays for a family, taken from the guardians already on its students' records. It refers to
    the guardian rather than copying their name and phone, so a correction on the student record is
    a correction here."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="+")
    family = models.ForeignKey(Family, on_delete=models.PROTECT, related_name="guardians")
    guardian = models.ForeignKey(GuardianLink, on_delete=models.PROTECT, related_name="family_links")
    is_primary_payer = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-is_primary_payer", "created_at", "id"]
        constraints = [
            models.UniqueConstraint(fields=["family", "guardian"], name="one_link_per_family_guardian"),
            models.UniqueConstraint(
                fields=["family"],
                condition=Q(is_primary_payer=True, is_active=True),
                name="one_primary_payer_per_family",
            ),
        ]

    def save(self, *args, **kwargs):
        if self.family.school_id != self.guardian.student.school_id:
            raise ValidationError("A family and its guardians must belong to the same school.")
        self.school_id = self.family.school_id
        super().save(*args, **kwargs)
