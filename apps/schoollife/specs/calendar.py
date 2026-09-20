"""What is on and when: events, assembly, excursions."""

from ..framework import LEADERS, MANAGERS, Spec

EVENTS = Spec(
    "school_event",
    manage=MANAGERS,
    contribute=frozenset({"teacher"}),
    required=("title", "date"),
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
    required=("title", "date"),
    guarded={"readinessReviewed": LEADERS},
    defaults={"readinessReviewed": False},
)

SPECS = [EVENTS, ASSEMBLY, EXCURSIONS]
