"""Official communication: the noticeboard."""

from ..framework import EVERYONE, LEADERS, MANAGERS, STAFF_SIDE, Spec


def _notice_audience(payload):
    # Notices for staff go to the staff side only; every other audience is the whole school
    # until sections and classes exist as records the server can check.
    return STAFF_SIDE if str(payload.get("audience", "")).lower().replace(" ", "") in ("staff", "staffonly") else None


NOTICEBOARD = Spec(
    "noticeboard_notice",
    manage=MANAGERS,
    read=EVERYONE,
    required=("title", "body"),
    # Pinning is the leadership's call. Read counts are the server's to keep, not a device's.
    guarded={"pinned": LEADERS},
    defaults={"pinned": False},
    server_owned={"readCount": 0, "totalRecipients": 0},
    audience=_notice_audience,
)

SPECS = [NOTICEBOARD]
