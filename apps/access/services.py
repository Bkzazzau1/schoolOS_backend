"""Who can see which activity, and how the owner changes that.

Effective access for a person is worked out from three layers, in order:

  1. the built-in defaults for their role (apps/access/catalog),
  2. the school's changes to that role (RoleActivity),
  3. the owner's grants and blocks for that person (MembershipActivity).

The owner always has everything an owner has, and nobody can be locked out of
their landing screen (an "essential" activity).

A block is normally **two steps**. The person's app first fetches the latest and
submits its pending work, then confirms (`acknowledge`), and only then does the
block take effect. If the app never confirms, it takes effect after
ACCESS_BLOCK_GRACE_HOURS anyway. The owner can also block immediately.

Every change tells the person affected (in-app; email comes when there is a mail
backend).
"""

from datetime import datetime, timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.notifications.services import notify, notify_many
from apps.schools.models import Membership, Role

from . import catalog
from .models import AccessChange, MembershipActivity, RoleActivity

PROPRIETOR = Role.PROPRIETOR.value
AFTER_SYNC = "after_sync"
IMMEDIATE = "immediate"
MODES = (AFTER_SYNC, IMMEDIATE)
NOTICE_KIND = "access_changed"
NOTICE_TITLE = "Your access changed"


class AccessError(Exception):
    """A change that is not allowed, with a message the owner can act on."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


# --- reading ------------------------------------------------------------------


def role_defaults(school, role: str) -> set[str]:
    """What a role has in this school: built-in defaults plus the school's changes."""
    keys = catalog.default_keys(role)
    for row in RoleActivity.objects.filter(school=school, role=role):
        (keys.add if row.enabled else keys.discard)(row.activity)
    return keys | catalog.essential_keys(role)


def active_overrides(membership: Membership, now: datetime | None = None) -> list[MembershipActivity]:
    """The person's grants and blocks that have not expired."""
    now = now or timezone.now()
    return [
        o
        for o in membership.activity_overrides.all()
        if o.expires_at is None or o.expires_at > now
    ]


def pending_blocks(membership: Membership, now: datetime | None = None) -> list[MembershipActivity]:
    """Blocks the person has been told about that are not yet in force. The person
    still has these activities; their app should fetch, submit, then acknowledge."""
    now = now or timezone.now()
    return [o for o in active_overrides(membership, now) if o.is_pending(now)]


def effective_activities(membership: Membership, now: datetime | None = None) -> set[str]:
    """Everything this person may see right now."""
    now = now or timezone.now()
    if membership.role == PROPRIETOR:
        return catalog.default_keys(PROPRIETOR)  # the owner is not configurable
    keys = role_defaults(membership.school, membership.role)
    for override in active_overrides(membership, now):
        if override.effect == MembershipActivity.Effect.GRANT:
            keys.add(override.activity)
        elif not override.is_pending(now):
            keys.discard(override.activity)
    return keys | catalog.essential_keys(membership.role)


def has_activity(membership: Membership, key: str) -> bool:
    return key in effective_activities(membership)


# --- telling people -----------------------------------------------------------


def _label(key: str) -> str:
    activity = catalog.get(key)
    return activity.label if activity else key


def _names(keys, limit: int = 5) -> str:
    labels = sorted(_label(k) for k in keys)
    shown = ", ".join(labels[:limit])
    return shown + (f" and {len(labels) - limit} more" if len(labels) > limit else "")


def _date(value: datetime) -> str:
    return timezone.localtime(value).strftime("%d %b %Y")


def _tell(member: Membership, message: str, note: str = "", **data) -> None:
    if note.strip():
        message += f" Note from the owner: {note.strip()}"
    notify(member, NOTICE_KIND, NOTICE_TITLE, message, data)


# --- writing (owner only; the caller has already confirmed the actor is the owner) ---


def _log(actor, school, kind, *, role="", target=None, activity="", detail=None):
    AccessChange.objects.create(
        school=school, actor=actor, kind=kind, role=role, target=target,
        activity=activity, detail=detail or {},
    )


def _require_owner(actor: Membership, school):
    if actor.role != PROPRIETOR or actor.school_id != school.id:
        raise AccessError("Only the school owner can change access.")


def _target(actor: Membership, membership_id) -> Membership:
    target = Membership.objects.select_related("school", "user").filter(
        id=membership_id, school=actor.school, is_active=True
    ).first()
    if target is None:
        raise AccessError("That person is not a member of this school.")
    if target.role == PROPRIETOR:
        raise AccessError("The owner's access cannot be changed.")
    return target


def _activity(key: str):
    activity = catalog.get(key)
    if activity is None:
        raise AccessError(f"'{key}' is not an activity.")
    return activity


@transaction.atomic
def set_person_override(
    actor: Membership,
    membership_id,
    key: str,
    effect: str,
    *,
    expires_at: datetime | None = None,
    note: str = "",
    mode: str = AFTER_SYNC,
) -> MembershipActivity:
    """Give someone an activity (grant) or take one away (block).

    A block takes effect after the person's app has synced (`mode="after_sync"`,
    the default) or straight away (`mode="immediate"`). A grant is always
    immediate.
    """
    _require_owner(actor, actor.school)
    target = _target(actor, membership_id)
    activity = _activity(key)
    now = timezone.now()
    if effect not in MembershipActivity.Effect.values:
        raise AccessError("Choose grant or block.")
    if mode not in MODES:
        raise AccessError("Choose whether the block waits for the person's next sync or is immediate.")
    if expires_at is not None and expires_at <= now:
        raise AccessError("The end date must be in the future.")
    if len(note) > 200:
        raise AccessError("The note is too long.")
    if effect == MembershipActivity.Effect.BLOCK and key in catalog.essential_keys(target.role):
        raise AccessError(
            f"{activity.label} is their landing screen and cannot be blocked, or they could not use the app."
        )
    if effect == MembershipActivity.Effect.GRANT and not activity.grantable:
        raise AccessError(f"{activity.label} can only ever be used by the owner.")

    had = has_activity(target, key)
    before = MembershipActivity.objects.filter(membership=target, activity=key).first()
    finalize_at = None
    # Waiting only makes sense if they have it now: a person who already lacks it
    # has no work to submit, and re-blocking must never bring it back for a while.
    if effect == MembershipActivity.Effect.BLOCK and mode == AFTER_SYNC and had:
        finalize_at = now + timedelta(hours=settings.ACCESS_BLOCK_GRACE_HOURS)
    row, _ = MembershipActivity.objects.update_or_create(
        membership=target,
        activity=key,
        defaults={
            "effect": effect, "expires_at": expires_at, "note": note.strip(), "set_by": actor,
            "finalize_at": finalize_at, "acknowledged_at": None,
        },
    )
    _log(
        actor, actor.school, f"person_{effect}", target=target, activity=key,
        detail={
            "was": before.effect if before else None,
            "mode": mode if effect == MembershipActivity.Effect.BLOCK else None,
            "expiresAt": expires_at.isoformat() if expires_at else None,
            "note": note.strip(),
        },
    )
    _announce_override(target, activity.label, key, effect, had, finalize_at, expires_at, note)
    return row


def _announce_override(target, label, key, effect, had, finalize_at, expires_at, note):
    """Tell the person, but only when something really changes for them."""
    data = {"activities": [key], "effect": effect}
    if effect == MembershipActivity.Effect.GRANT and not had:
        until = f" until {_date(expires_at)}" if expires_at else ""
        _tell(target, f"You can now use {label}{until}.", note, **data)
    elif effect == MembershipActivity.Effect.BLOCK and had:
        if finalize_at is not None:
            _tell(
                target,
                f"{label} will be removed from your account after your next sync, and no later than {_date(finalize_at)}. "
                "Your app will send any work you have not yet submitted first.",
                note, finalizeAt=finalize_at.isoformat(), **data,
            )
        else:
            _tell(target, f"You no longer have {label}.", note, **data)


@transaction.atomic
def clear_person_override(actor: Membership, membership_id, key: str) -> bool:
    """Put a person back on their role's default for one activity."""
    _require_owner(actor, actor.school)
    target = _target(actor, membership_id)
    _activity(key)
    row = MembershipActivity.objects.filter(membership=target, activity=key).first()
    if row is None:
        return False
    was_pending = row.is_pending(timezone.now())
    had = has_activity(target, key)
    row.delete()
    _log(actor, actor.school, "person_clear", target=target, activity=key, detail={"was": row.effect})
    now_has = has_activity(target, key)
    data = {"activities": [key]}
    if was_pending:
        _tell(target, f"{_label(key)} will stay on your account. The earlier notice no longer applies.", **data)
    elif now_has and not had:
        _tell(target, f"You can now use {_label(key)}.", **data)
    elif had and not now_has:
        _tell(target, f"You no longer have {_label(key)}.", **data)
    return True


@transaction.atomic
def acknowledge(membership: Membership, keys) -> list[str]:
    """The person's app has fetched the latest and submitted its work: let the
    pending blocks it names take effect now. Returns which ones did."""
    now = timezone.now()
    rows = MembershipActivity.objects.filter(
        membership=membership, activity__in=list(keys),
        effect=MembershipActivity.Effect.BLOCK, acknowledged_at__isnull=True, finalize_at__gt=now,
    )
    done = sorted(rows.values_list("activity", flat=True))
    rows.update(acknowledged_at=now)
    for key in done:
        _log(membership, membership.school, "block_acknowledged", target=membership, activity=key)
    return done


@transaction.atomic
def reassign(actor: Membership, key: str, from_id, to_id, *, note: str = "", mode: str = AFTER_SYNC) -> None:
    """Move an activity from one person to another in one step.

    The first person loses it (after their next sync, unless `mode="immediate"`)
    and the second gains it at once, or neither changes.
    """
    _require_owner(actor, actor.school)
    if str(from_id) == str(to_id):
        raise AccessError("Choose two different people.")
    giver, receiver = _target(actor, from_id), _target(actor, to_id)
    activity = _activity(key)
    if not has_activity(giver, key):
        raise AccessError(f"{giver.user.email} does not have {activity.label}.")
    if has_activity(receiver, key):
        raise AccessError(f"{receiver.user.email} already has {activity.label}.")
    set_person_override(actor, giver.id, key, MembershipActivity.Effect.BLOCK, note=note, mode=mode)
    set_person_override(actor, receiver.id, key, MembershipActivity.Effect.GRANT, note=note)
    _log(actor, actor.school, "reassign", target=receiver, activity=key,
         detail={"from": str(giver.id), "to": str(receiver.id), "note": note.strip()})


def _announce_role_change(school, role: str, before: set[str], after: set[str]) -> None:
    added, removed = after - before, before - after
    if not added and not removed:
        return
    parts = []
    if added:
        parts.append(f"You can now use: {_names(added)}.")
    if removed:
        parts.append(f"No longer available: {_names(removed)}.")
    people = Membership.objects.filter(school=school, role=role, is_active=True).select_related("school")
    notify_many(
        people, NOTICE_KIND, NOTICE_TITLE, " ".join(parts),
        {"role": role, "added": sorted(added), "removed": sorted(removed)},
    )


@transaction.atomic
def set_role_defaults(actor: Membership, role: str, keys: set[str]) -> set[str]:
    """Decide exactly which activities a role gets in this school."""
    school = actor.school
    _require_owner(actor, school)
    if role not in Role.values:
        raise AccessError("That is not a role.")
    if role == PROPRIETOR:
        raise AccessError("The owner's activities cannot be changed.")
    unknown = sorted(k for k in keys if catalog.get(k) is None)
    if unknown:
        raise AccessError(f"'{unknown[0]}' is not an activity.")
    builtin = catalog.default_keys(role)
    missing = sorted(catalog.essential_keys(role) - keys)
    if missing:
        raise AccessError(f"{catalog.get(missing[0]).label} is the landing screen and cannot be removed.")
    for key in sorted(keys - builtin):
        if not catalog.get(key).grantable:
            raise AccessError(f"{catalog.get(key).label} can only ever be used by the owner.")

    before = role_defaults(school, role)
    RoleActivity.objects.filter(school=school, role=role).delete()
    RoleActivity.objects.bulk_create(
        [RoleActivity(school=school, role=role, activity=k, enabled=True) for k in sorted(keys - builtin)]
        + [RoleActivity(school=school, role=role, activity=k, enabled=False) for k in sorted(builtin - keys)]
    )
    after = role_defaults(school, role)
    _log(actor, school, "role_set", role=role, detail={
        "added": sorted(after - before), "removed": sorted(before - after),
    })
    _announce_role_change(school, role, before, after)
    return after


@transaction.atomic
def reset_role_defaults(actor: Membership, role: str) -> set[str]:
    """Undo the school's changes to a role and go back to the built-in defaults."""
    _require_owner(actor, actor.school)
    if role not in Role.values or role == PROPRIETOR:
        raise AccessError("That role's activities cannot be changed.")
    before = role_defaults(actor.school, role)
    removed = RoleActivity.objects.filter(school=actor.school, role=role).count()
    RoleActivity.objects.filter(school=actor.school, role=role).delete()
    after = role_defaults(actor.school, role)
    _log(actor, actor.school, "role_reset", role=role, detail={"changes_removed": removed})
    _announce_role_change(actor.school, role, before, after)
    return after
