"""Official communication: the noticeboard, and a family's own class channel."""

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

# A guardian's message toward their child's class channel. No real school-to-guardian sending
# exists yet (the app is honest that every message here is guardian-authored so far), so this is
# one-directional today: a parent writes, and the whole staff side can read - the class teacher
# among them. Leadership can manage/moderate a channel; a parent may only change their own message.
PARENT_MESSAGE = Spec(
    "parent_message",
    manage=MANAGERS,
    contribute=frozenset({"parent"}),
    read=STAFF_SIDE,
    id_field="messageId",
    required=("messageId", "threadId", "body"),
)

SPECS = [NOTICEBOARD, PARENT_MESSAGE]
