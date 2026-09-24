import uuid

from django.contrib.auth.base_user import BaseUserManager
from django.contrib.auth.models import AbstractUser
from django.conf import settings
from django.db import models


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create(self, email, password, **extra):
        if not email:
            raise ValueError("An email address is required.")
        user = self.model(email=self.normalize_email(email).lower(), **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra):
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        return self._create(email, password, **extra)

    def create_superuser(self, email, password=None, **extra):
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        return self._create(email, password, **extra)


class User(AbstractUser):
    """A person who signs in.

    Which schools they belong to, and in what role, is held by
    ``schools.Membership`` so one person can hold several. Email verification is
    account-level trust state and therefore belongs here rather than inside any
    school tenant.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    username = None
    email = models.EmailField(unique=True)

    # System-provisioned student/parent accounts start with the school's simple
    # bootstrap password rule and must replace it after their first successful
    # sign-in. Existing staff/owner accounts are never reset when a new school
    # membership is attached to them.
    must_change_password = models.BooleanField(default=False)

    # Verification codes are never stored in plaintext. These fields hold only
    # the digest and lifecycle metadata for the currently active code.
    email_verified_at = models.DateTimeField(null=True, blank=True)
    email_verification_code_hash = models.CharField(max_length=64, blank=True)
    email_verification_expires_at = models.DateTimeField(null=True, blank=True)
    email_verification_sent_at = models.DateTimeField(null=True, blank=True)
    email_verification_attempts = models.PositiveSmallIntegerField(default=0)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS: list[str] = []

    objects = UserManager()

    @property
    def is_email_verified(self) -> bool:
        return self.email_verified_at is not None

    def __str__(self):
        return self.email


class LoginIdentityKind(models.TextChoices):
    STUDENT_ADMISSION = "student_admission", "Student admission ID"
    PARENT_PHONE = "parent_phone", "Parent phone number"


class LoginIdentity(models.Model):
    """A non-email credential name that resolves to one SchoolOS user.

    Email remains the normal account identity for proprietors/staff. Canonical
    student activation adds the admission-number identity; guardian provisioning
    adds the normalized phone identity. ``normalized_identifier`` is globally
    unique per kind because sign-in happens before a school tenant is selected.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="login_identities",
    )
    kind = models.CharField(max_length=32, choices=LoginIdentityKind.choices)
    identifier = models.CharField(max_length=160)
    normalized_identifier = models.CharField(max_length=160)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["kind", "normalized_identifier"],
                name="unique_login_identity_by_kind",
            )
        ]
        indexes = [
            models.Index(fields=["user", "kind"], name="login_identity_user_kind_idx")
        ]

    def __str__(self):
        return f"{self.kind} · {self.identifier}"
