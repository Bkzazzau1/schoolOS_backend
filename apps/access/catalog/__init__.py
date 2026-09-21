"""Every activity in the app, and who has each by default.

The catalog is code, not database rows, so the app and the server share one list
and a typo cannot create an activity. The database stores only what differs
from it: a school's changes to a role's defaults, and a person's grants and
blocks (see models.py).
"""

from .base import Activity
from .family_and_general import GENERAL, PARENT, SCHOOL_LIFE
from .owner import OWNER
from .staff_workspaces import ADMINISTRATOR, DRIVER, FINANCE, PRINCIPAL, TEACHER

ACTIVITIES: dict[str, Activity] = {}
for _group in (OWNER, PRINCIPAL, ADMINISTRATOR, FINANCE, TEACHER, DRIVER, PARENT, SCHOOL_LIFE, GENERAL):
    for _activity in _group:
        if _activity.key in ACTIVITIES:
            raise ValueError(f"Duplicate activity key: {_activity.key}")
        ACTIVITIES[_activity.key] = _activity

#: The workspaces in display order, for grouping.
GROUPS = [OWNER, PRINCIPAL, ADMINISTRATOR, FINANCE, TEACHER, DRIVER, PARENT, SCHOOL_LIFE, GENERAL]


def get(key: str) -> Activity | None:
    return ACTIVITIES.get(key)


def default_keys(role: str) -> set[str]:
    """What a role has out of the box, before any school changes."""
    return {a.key for a in ACTIVITIES.values() if role in a.default_roles}


def essential_keys(role: str) -> set[str]:
    """What can never be taken from this role or from a person in it."""
    return {a.key for a in ACTIVITIES.values() if a.essential and role in a.default_roles}
