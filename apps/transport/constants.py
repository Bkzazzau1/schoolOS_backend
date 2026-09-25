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

#: School management: sets policy (routes, stops, driver/rider assignments, vehicle clearance).
MANAGERS = frozenset({"proprietor", "administrator"})
#: Adds read-only oversight to MANAGERS.
READERS = MANAGERS | {"principal"}

TIME_PATTERN = r"^([01]\d|2[0-3]):[0-5]\d$"

CLEARANCE_STATUSES = ("released", "held", "maintenance")
