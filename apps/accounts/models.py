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

    # Credential-sensitive administrative actions increment this value. SkulGen
    # JWTs carry the version they were issued under, so a password reset or login
    # phone change can invalidate already-issued Student/Parent sessions.
    credential_version = models.PositiveIntegerField(default=1)

    # Verification codes are never stored in plaintext. These fields hold only
    # the digest and lifecycle metadata for the currently active code.
    email_verified_at = models.DateTimeField(null=True, blank=True)
    email_verification_code_hash = models.CharField(max_length=64, blank=True)
    email_verification_expires_at = models.DateTimeField(null=True, blank=True)
    email_verification_sent_at = models.DateTimeField(null=True, blank=True)
    email_verification_attempts = models.PositiveSmallIntegerField(default=0)

    # Forgotten-password recovery for an email-login (Staff/Proprietor) account: a
    # short code sent to this same address, the same shape as email verification
    # above. Student and Parent accounts sign in with an admission ID or phone and
    # recover through the school office instead (apps.credentials.services).
    password_reset_code_hash = models.CharField(max_length=64, blank=True)
    password_reset_expires_at = models.DateTimeField(null=True, blank=True)
    password_reset_sent_at = models.DateTimeField(null=True, blank=True)
    password_reset_attempts = models.PositiveSmallIntegerField(default=0)

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
    """A non-email credential name that resolves to one SkulGen user.

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


class DevicePlatform(models.TextChoices):
    ANDROID = "android", "Android"
    IOS = "ios", "iOS"
    WINDOWS = "windows", "Windows"
    WEB = "web", "Web"


class DeviceToken(models.Model):
    """One push-notification token for one signed-in device.

    Keyed on the account (``User``), not a school membership: the same phone has
    one real push channel no matter how many school roles its owner holds (see
    ``apps.notifications.models.Notification``'s own docstring on a person
    having a separate inbox per membership - push does not follow that split).
    ``token`` is globally unique because it names one physical app install, not
    one account; registering a token already pointed at a different account
    (a shared device changing hands) reassigns it rather than erroring, since
    the old account could never genuinely use it again anyway.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="device_tokens",
    )
    platform = models.CharField(max_length=16, choices=DevicePlatform.choices)
    token = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["token"], name="unique_device_token"),
        ]
        indexes = [
            models.Index(fields=["user", "platform"], name="device_token_user_platform_idx"),
        ]

    def __str__(self):
        return f"{self.platform} · {self.user_id}"
