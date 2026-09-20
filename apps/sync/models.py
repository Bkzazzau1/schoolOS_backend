from django.db import models, transaction

from apps.schools.models import Membership, School


class SyncRecord(models.Model):
    """The server's copy of one record the app syncs, identified the same way
    the app identifies it locally: school, entity type and entity id.

    `version` goes up by one on every accepted change. The app sends the
    version it last saw as `baseVersion`, which is how a concurrent edit from
    another device is detected."""

    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="sync_records")
    entity_type = models.CharField(max_length=64)
    entity_id = models.CharField(max_length=128)
    payload = models.JSONField(default=dict)
    version = models.PositiveIntegerField(default=1)
    deleted = models.BooleanField(default=False)
    updated_by = models.ForeignKey(
        Membership, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    updated_at = models.DateTimeField(auto_now=True)
    #: The school's change counter at the moment of the last change. Devices ask
    #: for "everything after the number I last saw", so this must go up on every
    #: change and never repeat within a school (see next_seq).
    seq = models.PositiveBigIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["school", "entity_type", "entity_id"], name="unique_sync_record"
            ),
            models.UniqueConstraint(fields=["school", "seq"], name="unique_sync_seq"),
        ]
        indexes = [models.Index(fields=["school", "entity_type"])]

    def save(self, *args, **kwargs):
        # Every save is a change devices must hear about, so it gets the next number.
        with transaction.atomic():
            self.seq = next_seq(self.school_id)
            if kwargs.get("update_fields") is not None:
                kwargs["update_fields"] = {*kwargs["update_fields"], "seq"}
            super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.entity_type}/{self.entity_id} v{self.version}"


class MutationLog(models.Model):
    """Every mutation the server has decided on, accepted or not.

    It makes retries safe: if the app resends a mutation because the response
    was lost, the same decision is returned instead of applying it twice. It is
    also the audit trail of who changed what."""

    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="mutation_logs")
    mutation_id = models.CharField(max_length=64)
    membership = models.ForeignKey(Membership, on_delete=models.PROTECT, related_name="+")
    entity_type = models.CharField(max_length=64)
    entity_id = models.CharField(max_length=128)
    operation = models.CharField(max_length=10)
    disposition = models.CharField(max_length=10)
    server_version = models.PositiveIntegerField(null=True, blank=True)
    message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["school", "mutation_id"], name="unique_mutation_id")
        ]
        indexes = [models.Index(fields=["school", "entity_type", "entity_id"])]

    def __str__(self):
        return f"{self.mutation_id} {self.disposition}"


class SchoolSequence(models.Model):
    """The last change number handed out in a school."""

    school = models.OneToOneField(School, on_delete=models.CASCADE, related_name="+")
    last = models.PositiveBigIntegerField(default=0)


def next_seq(school_id) -> int:
    """The next change number for a school.

    The counter row is locked until the surrounding transaction commits, so two
    changes in one school are numbered and committed strictly in order. That is
    what makes "everything after number N" safe: a change with a lower number can
    never appear after a device has already read past it.
    """
    with transaction.atomic():
        SchoolSequence.objects.get_or_create(school_id=school_id)
        counter = SchoolSequence.objects.select_for_update().get(school_id=school_id)
        counter.last += 1
        counter.save(update_fields=["last"])
        return counter.last
