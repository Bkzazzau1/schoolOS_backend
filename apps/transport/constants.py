"""Values this feature shares with the app. Each name mirrors the app's own entity type constant."""

ROUTE = "school_transport_route"  # apps.schoollife already owns this one; referenced, not registered here.

DRIVER_ASSIGNMENT = "driver_transport_assignment"
ASSIGNMENT_EVENT = "transport_assignment_event"

RIDER_ASSIGNMENT = "transport_rider_assignment"
RIDER_ASSIGNMENT_EVENT = "transport_rider_assignment_event"

ROUTE_PLAN = "transport_route_plan"
ROUTE_EVENT = "transport_route_event"

VEHICLE_CLEARANCE = "transport_vehicle_clearance"
VEHICLE_EVENT = "transport_vehicle_event"

#: A Driver's own morning/afternoon run or vehicle check, keyed the same way the app keys them
#: locally (membershipId:period:serviceDate). Referenced to lock an assignment once today's
#: service has started - these entity types belong to a later phase and may not have any rows
#: yet, which correctly means nothing is locked until that phase ships.
MORNING_RUN = "driver_morning_run"
AFTERNOON_RUN = "driver_afternoon_run"
VEHICLE_CHECK = "driver_vehicle_check"
VEHICLE_DEFECT = "driver_vehicle_defect"

TRANSPORT_EVENT = "driver_transport_event"

INCIDENT = "driver_transport_incident"
CASE_EVENT = "transport_case_event"

DRIVER_MESSAGE = "driver_message"
DRIVER_MESSAGE_RECEIPT = "driver_message_receipt"
DRIVER_ALERT_RECEIPT = "driver_alert_receipt"

INCIDENT_CATEGORIES = (
    "vehicleBreakdown", "accident", "trafficDelay", "routeObstruction", "studentNotAtStop",
    "guardianUnavailable", "studentUnwell", "vehicleIssue", "safetyConcern", "other",
)
INCIDENT_SEVERITIES = ("low", "medium", "high", "critical")
INCIDENT_PHASES = (
    "beforeMorningRun", "morningRoute", "atSchool", "beforeAfternoonRun", "afternoonRoute", "afterService",
)
#: A manager moves an incident forward through these; "queued"/"submitted" are the driver's side.
INCIDENT_REVIEW_STATUSES = ("acknowledged", "underReview", "resolved")

#: The status words the app uses on a vehicle defect. Closed ones can never be reopened.
DEFECT_STATUSES = ("reported", "acknowledged", "under_review", "cleared", "resolved", "closed")
DEFECT_CLOSED = frozenset({"cleared", "resolved", "closed"})

#: The fixed catalog of vehicle check items every check is built from, and each one's
#: severity (see driver_vehicle_check_demo_data.dart) - a check can't invent a new item,
#: and severity is the server's own truth, never taken from the device: it decides
#: whether a failure blocks the trip, so a device must not be able to soften it.
VEHICLE_CHECK_ITEM_SEVERITY = {
    "fuel_charge": "critical", "tyres": "critical", "brakes": "critical",
    "steering": "critical", "lights_horn": "critical", "doors": "critical",
    "seat_belts": "critical", "warning_lights": "critical", "fire_extinguisher": "critical",
    "first_aid": "critical", "interior": "advisory",
}
VEHICLE_CHECK_ITEM_IDS = frozenset(VEHICLE_CHECK_ITEM_SEVERITY)

#: School management: sets policy (routes, stops, driver/rider assignments, vehicle clearance).
MANAGERS = frozenset({"proprietor", "administrator"})
#: Adds read-only oversight to MANAGERS.
READERS = MANAGERS | {"principal"}

TIME_PATTERN = r"^([01]\d|2[0-3]):[0-5]\d$"

CLEARANCE_STATUSES = ("released", "held", "maintenance")
