"""Running the campus: transport, meals, boarding, visitors, lost and found, gallery."""

from ..framework import LEADERS, MANAGERS, STAFF_SIDE, Spec

TRANSPORT = Spec(
    "school_transport_route",
    manage=MANAGERS,
    required=("name",),
    guarded={"reviewed": LEADERS},
    defaults={"reviewed": False},
)

# One record per weekday; the id is the lower-case day.
MEALS = Spec("school_meal_day", manage=MANAGERS, id_field="day", required=("day",))

# Boarders' welfare (leave, maintenance): the staff side only. The id is made from the dorm name.
BOARDING = Spec(
    "boarding_dorm",
    manage=MANAGERS,
    read=STAFF_SIDE,
    id_field=None,
    required=("name",),
    guarded={"handoverReviewed": LEADERS},
    defaults={"handoverReviewed": False},
)

# Who came to the school: the front desk logs it, and only the staff side sees the log.
VISITORS = Spec(
    "visitor_record",
    manage=MANAGERS,
    contribute=frozenset({"staff"}),
    read=STAFF_SIDE,
    required=("visitor",),
    guarded={"frontDeskReviewed": MANAGERS},
    defaults={"frontDeskReviewed": False},
)

# Anyone may report a found item; only staff who run the office settle a claim.
LOST_FOUND = Spec(
    "lost_found_item",
    manage=MANAGERS | {"staff"},
    contribute=frozenset({"teacher", "parent", "student", "accountant", "driver"}),
    required=("item",),
    guarded={"claimant": MANAGERS | {"staff"}, "status": MANAGERS | {"staff"}},
)


def _gallery_audience(payload):
    return STAFF_SIDE if payload.get("visibility") == "internal" else None


GALLERY = Spec(
    "gallery_media_album",
    manage=MANAGERS,
    contribute=frozenset({"teacher", "staff"}),
    required=("title",),
    # Publishing pictures of children outside the school is the leadership's decision.
    guarded_values={("visibility", "publicShowcase"): LEADERS},
    audience=_gallery_audience,
)

SPECS = [TRANSPORT, MEALS, BOARDING, VISITORS, LOST_FOUND, GALLERY]
