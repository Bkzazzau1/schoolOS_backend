"""What is on and when: events, assembly, excursions."""

from ..framework import LEADERS, MANAGERS, Spec

EVENTS = Spec(
    "school_event",
    manage=MANAGERS,
    contribute=frozenset({"teacher"}),
    required=("title", "date"),
    # Declaring a day non-instructional is the leadership's call, not any contributing teacher's -
    # other real features (attendance expectations) act on this, unlike every other event "type".
    guarded_values={("type", "holiday"): MANAGERS},
)

ASSEMBLY = Spec(
    "assembly_session",
    manage=MANAGERS,
    contribute=frozenset({"teacher"}),
    required=("title",),
)

EXCURSIONS = Spec(
    "school_excursion",
    manage=MANAGERS,
    contribute=frozenset({"teacher"}),
    # termId ties the trip to a real academic term rather than a free-text label.
    required=("title", "date", "termId"),
    guarded={"readinessReviewed": LEADERS},
    defaults={"readinessReviewed": False},
)

SPECS = [EVENTS, ASSEMBLY, EXCURSIONS]
