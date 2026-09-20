"""Telling people things. Any feature can call these.

    from apps.notifications.services import notify

    notify(membership, "access_changed", "Your access changed", "You can now use Payroll.")

This puts the message in the person's in-app inbox. Email is not sent yet (there
is no mail backend); when there is, it is added here so every feature gets it.
"""

from collections.abc import Iterable

from django.utils import timezone

from apps.schools.models import Membership

from .models import Notification


def notify(recipient: Membership, kind: str, title: str, message: str, data: dict | None = None) -> Notification:
    return Notification.objects.create(
        school=recipient.school, recipient=recipient, kind=kind,
        title=title[:120], message=message, data=data or {},
    )


def notify_many(
    recipients: Iterable[Membership], kind: str, title: str, message: str, data: dict | None = None
) -> int:
    """The same message to several people. Returns how many were told."""
    rows = [
        Notification(
            school=r.school, recipient=r, kind=kind, title=title[:120], message=message, data=data or {}
        )
        for r in recipients
    ]
    Notification.objects.bulk_create(rows)
    return len(rows)


def mark_read(recipient: Membership, notification_id) -> bool:
    """Mark one of the person's own messages read. False if it is not theirs."""
    mine = Notification.objects.filter(id=notification_id, recipient=recipient)
    if not mine.exists():
        return False
    mine.filter(read_at__isnull=True).update(read_at=timezone.now())
    return True


def mark_all_read(recipient: Membership) -> int:
    return Notification.objects.filter(recipient=recipient, read_at__isnull=True).update(read_at=timezone.now())
