import secrets

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from apps.schools.models import School

from .hostnames import DomainError, validate_for_kind


class SchoolDomain(models.Model):
    """A web address that belongs to one school.

    Every school gets a **platform** subdomain automatically (for example
    brightgate.schoolos.ng), which is what lets the phone app open links
    directly. A school may also add a **custom** domain it owns. A custom domain
    only counts once its owner proves control by adding a DNS record.

    The **primary** domain is the one used in emailed links. A domain must be
    verified before it can be primary, and a school has at most one.
    """

    class Kind(models.TextChoices):
        PLATFORM = "platform"
        CUSTOM = "custom"

    class Status(models.TextChoices):
        PENDING = "pending"    # custom domain not yet proven
        VERIFIED = "verified"
        DISABLED = "disabled"

    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="domains")
    host = models.CharField(max_length=253, unique=True)
    kind = models.CharField(max_length=10, choices=Kind.choices)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    is_primary = models.BooleanField(default=False)
    #: What the owner puts in a DNS TXT record to prove control (custom only).
    verification_token = models.CharField(max_length=64, blank=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["school__name", "-is_primary", "host"]
        constraints = [
            models.UniqueConstraint(
                fields=["school"], condition=Q(is_primary=True), name="one_primary_domain_per_school"
            ),
            models.CheckConstraint(
                condition=Q(is_primary=False) | Q(status="verified"),
                name="primary_domain_must_be_verified",
            ),
        ]

    def __str__(self):
        return self.host

    @property
    def dns_record_name(self) -> str:
        return f"_schoolos-verify.{self.host}"

    def clean(self):
        try:
            self.host = validate_for_kind(self.host, self.kind)
        except DomainError as error:
            raise ValidationError({"host": error.message})

    def save(self, *args, **kwargs):
        # Check a host when it is first saved. Existing rows are not re-checked, so
        # changing the platform domain later cannot make them unsaveable.
        if self._state.adding:
            self.host = validate_for_kind(self.host, self.kind)
        if self.kind == self.Kind.CUSTOM and not self.verification_token:
            self.verification_token = secrets.token_urlsafe(24)
        super().save(*args, **kwargs)
