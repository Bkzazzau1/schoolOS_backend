from django.db import models

from apps.schools.models import Membership, School


class Notification(models.Model):
    """A message to one person, shown in the app.

    It belongs to a **membership**, not a user: someone who is a teacher at one
    school and a parent at another has separate inboxes. `kind` and `data` are
    for the app to act on (for example, re-read access when kind is
    "access_changed"); `title` and `message` are for the person to read.
    """

    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="notifications")
    recipient = models.ForeignKey(Membership, on_delete=models.CASCADE, related_name="notifications")
    kind = models.CharField(max_length=40)
    title = models.CharField(max_length=120)
    message = models.TextField()
    data = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["recipient", "read_at", "-created_at"])]

    def __str__(self):
        return f"{self.kind} to {self.recipient_id}"
