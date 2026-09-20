"""What the four community record types share.

Community is the one module everyone writes to, so each thing a person does is its
own record: a post, a comment on it, a reaction to it, a report about it. That way
nobody ever edits someone else's record, and two people commenting at once cannot
overwrite each other. (The app used to keep comments and a reaction count inside the
post itself, which cannot be made safe on a shared server.)
"""

from apps.schoollife.framework import EVERYONE, MANAGERS, STAFF_SIDE
from apps.sync.models import SyncRecord

POST = "community_post"
COMMENT = "community_comment"
REACTION = "community_reaction"
REPORT = "community_report"

#: Children read the community but do not post to it, until the school decides otherwise.
WRITERS = EVERYONE - {"student"}
MODERATORS = MANAGERS
AUDIENCES = ("wholeSchool", "earlyYears", "primary", "secondary", "staffOnly", "parentsOnly", "jss2A")
VISIBILITIES = ("schoolOnly", "publicShowcase")

MAX_TEXT = 2000


def display_name(membership) -> str:
    user = membership.user
    return f"{user.first_name} {user.last_name}".strip() or user.email.split("@")[0]


def role_label(membership) -> str:
    return "Owner" if membership.role == "proprietor" else membership.role.title()


def may_see_post(membership, post: dict) -> bool:
    """Whether this person may see a post: its author and the moderators always can."""
    if membership.role in MODERATORS or post.get("authorMembershipId") == str(membership.id):
        return True
    if membership.role not in EVERYONE:
        return False
    audience = post.get("audience")
    if audience == "staffOnly":
        return membership.role in STAFF_SIDE
    if audience == "parentsOnly":
        return membership.role == "parent"
    return True


def find_post(school, post_id: str):
    """The stored post record (not deleted), or None."""
    return SyncRecord.objects.filter(school=school, entity_type=POST, entity_id=post_id, deleted=False).first()


def cached_post(membership, post_id: str) -> dict | None:
    """The post's payload, looked up once per request for the same person."""
    cache = membership.__dict__.setdefault("_post_cache", {})
    if post_id not in cache:
        record = find_post(membership.school, post_id)
        cache[post_id] = record.payload if record else None
    return cache[post_id]


