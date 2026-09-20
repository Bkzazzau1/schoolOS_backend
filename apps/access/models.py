from django.db import models

from apps.schools.models import Membership, School


class RoleActivity(models.Model):
    """A school's change to what a role gets by default.

    Only differences from the built-in defaults are stored: `enabled=True` adds
    an activity a role would not otherwise have, `enabled=False` takes one away.
    Deleting the row restores the built-in default.
    """

    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="role_activities")
    role = models.CharField(max_length=20)
    activity = models.CharField(max_length=64)
    enabled = models.BooleanField()
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["school", "role", "activity"], name="unique_role_activity")
        ]


class MembershipActivity(models.Model):
    """The owner's decision about one activity for one person.

    A **grant** gives them something their role does not have. A **block** takes
    away something it does. Either can expire, for example to cover someone while
    a colleague is on leave. Deleting the row puts the person back on their role's
    defaults.
    """

    class Effect(models.TextChoices):
        GRANT = "grant"
        BLOCK = "block"

    membership = models.ForeignKey(
        Membership, on_delete=models.CASCADE, related_name="activity_overrides"
    )
    activity = models.CharField(max_length=64)
    effect = models.CharField(max_length=5, choices=Effect.choices)
    expires_at = models.DateTimeField(null=True, blank=True)
    note = models.CharField(max_length=200, blank=True)
    #: A block made "after sync" does not take effect until the person's app has
    #: fetched the latest and submitted its pending work and told us so
    #: (`acknowledged_at`), or until this time passes, whichever is first. Null
    #: means it took effect immediately. Only used for blocks.
    finalize_at = models.DateTimeField(null=True, blank=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    set_by = models.ForeignKey(
        Membership, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    set_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["membership", "activity"], name="unique_person_activity")
        ]

    def is_pending(self, now) -> bool:
        """A block the person has been told about but that is not yet in force."""
        return (
            self.effect == self.Effect.BLOCK
            and self.acknowledged_at is None
            and self.finalize_at is not None
            and self.finalize_at > now
        )


class AccessChange(models.Model):
    """Who changed what, and when. Written for every change and never edited."""

    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="access_changes")
    actor = models.ForeignKey(Membership, null=True, on_delete=models.SET_NULL, related_name="+")
    kind = models.CharField(max_length=24)
    role = models.CharField(max_length=20, blank=True)
    target = models.ForeignKey(Membership, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    activity = models.CharField(max_length=64, blank=True)
    detail = models.JSONField(default=dict)
    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-at", "-id"]
        indexes = [models.Index(fields=["school", "-at"])]
