"""Telling people things. Any feature can call these.

    from apps.notifications.services import notify

    notify(membership, "access_changed", "Your access changed", "You can now use Payroll.")

This puts the message in the person's in-app inbox, and - best effort, through
push_delivery - on any device they have registered. Email is not sent yet
(there is no mail backend); when there is, it is added here so every feature
gets it.
"""

from collections.abc import Iterable

from django.utils import timezone

from apps.schools.models import Membership

from . import push_delivery
from .models import Notification


def notify(recipient: Membership, kind: str, title: str, message: str, data: dict | None = None) -> Notification:
    notification = Notification.objects.create(
        school=recipient.school, recipient=recipient, kind=kind,
        title=title[:120], message=message, data=data or {},
    )
    _push_best_effort([(recipient, notification)])
    return notification


def notify_many(
    recipients: Iterable[Membership], kind: str, title: str, message: str, data: dict | None = None
) -> int:
    """The same message to several people. Returns how many were told."""
    recipients = list(recipients)
    rows = [
        Notification(
            school=r.school, recipient=r, kind=kind, title=title[:120], message=message, data=data or {}
        )
        for r in recipients
    ]
    Notification.objects.bulk_create(rows)
    _push_best_effort(list(zip(recipients, rows)))
    return len(rows)


def _push_best_effort(pairs: list[tuple[Membership, Notification]]) -> None:
    """Push is a convenience on top of the in-app inbox above, never a condition for it:
    a missing device token, an unconfigured provider, or a genuine provider failure never
    stops (or rolls back) the Notification rows already created."""
    from apps.accounts.models import DeviceToken

    if not pairs:
        return
    tokens_by_user: dict = {}
    rows = DeviceToken.objects.filter(user_id__in={r.user_id for r, _n in pairs}).values("user_id", "token")
    for row in rows:
        tokens_by_user.setdefault(row["user_id"], []).append(row["token"])
    if not tokens_by_user:
        return
    provider = push_delivery.get_push_provider()
    for recipient, notification in pairs:
        tokens = tokens_by_user.get(recipient.user_id)
        if not tokens:
            continue
        try:
            provider.send(
                tokens=tokens, title=notification.title, body=notification.message,
                data={"notificationId": str(notification.id), "kind": notification.kind},
            )
        except push_delivery.DeliveryError:
            continue


def mark_read(recipient: Membership, notification_id) -> bool:
    """Mark one of the person's own messages read. False if it is not theirs."""
    mine = Notification.objects.filter(id=notification_id, recipient=recipient)
    if not mine.exists():
        return False
    mine.filter(read_at__isnull=True).update(read_at=timezone.now())
    return True


def mark_all_read(recipient: Membership) -> int:
    return Notification.objects.filter(recipient=recipient, read_at__isnull=True).update(read_at=timezone.now())
