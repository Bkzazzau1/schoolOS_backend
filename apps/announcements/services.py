from django.db import models as django_models
from django.utils import timezone

from .models import PlatformAnnouncement


def _time_window_q(as_of, *, from_field: str, until_field: str):
    return django_models.Q(**{f"{from_field}__lte": as_of}) & (
        django_models.Q(**{f"{until_field}__isnull": True}) | django_models.Q(**{f"{until_field}__gt": as_of})
    )


def active_announcements_for_organization(organization, *, as_of=None):
    """Every announcement currently live for this organization's schools - platform-wide
    ones (organization is null) and ones targeted at this organization specifically,
    newest first."""

    as_of = as_of or timezone.now()
    return (
        PlatformAnnouncement.objects.filter(
            django_models.Q(organization=organization) | django_models.Q(organization__isnull=True)
        )
        .filter(_time_window_q(as_of, from_field="effective_from", until_field="effective_until"))
        .order_by("-effective_from", "-id")
    )


def create_announcement(
    *,
    title: str,
    body: str,
    organization=None,
    effective_from=None,
    effective_until=None,
    actor=None,
) -> PlatformAnnouncement:
    title = title.strip()
    body = body.strip()
    if not title:
        raise ValueError("A title is required.")
    if not body:
        raise ValueError("A message is required.")
    effective_from = effective_from or timezone.now()
    if effective_until is not None and effective_until <= effective_from:
        raise ValueError("effective_until must be after effective_from.")

    return PlatformAnnouncement.objects.create(
        title=title,
        body=body,
        organization=organization,
        effective_from=effective_from,
        effective_until=effective_until,
        created_by=actor,
    )


def end_announcement(announcement: PlatformAnnouncement, *, actor=None) -> PlatformAnnouncement:
    """Ends an announcement from this moment forward. Never deletes the row - it stays
    on record as what was actually sent, and when."""

    if announcement.effective_until is not None and announcement.effective_until <= timezone.now():
        raise ValueError("This announcement has already ended.")
    announcement.effective_until = timezone.now()
    announcement.save(update_fields=["effective_until"])
    return announcement
