"""Co-curricular and belonging: activities, houses, service, awards, teaching models."""

from ..framework import LEADERS, MANAGERS, STAFF_SIDE, Spec

ACTIVITIES = Spec(
    "school_activity",
    manage=MANAGERS,
    contribute=frozenset({"teacher"}),
    required=("name",),
)

# House points and captains are the school's to set.
HOUSES = Spec("school_house", manage=MANAGERS, required=("name",))

SERVICE = Spec(
    "service_project",
    manage=MANAGERS,
    contribute=frozenset({"teacher"}),
    required=("title",),
    guarded={"verified": LEADERS},
    defaults={"verified": False},
)


def _award_audience(payload):
    return STAFF_SIDE if payload.get("visibility") == "internalOnly" else None


AWARDS = Spec(
    "award_recognition",
    manage=LEADERS,
    contribute=frozenset({"teacher"}),      # a teacher may draft; leadership publishes
    required=("title", "recipient"),
    guarded_values={("visibility", "publicShowcase"): LEADERS},
    audience=_award_audience,
)

TEACHING_MODELS = Spec(
    "teaching_model_config",
    manage=LEADERS,
    read=STAFF_SIDE,
    required=("section", "model"),
)

SPECS = [ACTIVITIES, HOUSES, SERVICE, AWARDS, TEACHING_MODELS]
