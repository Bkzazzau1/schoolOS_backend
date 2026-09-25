"""Every sync handler the transport feature provides (Phase 1: Transport Control's own
policy records - driver and rider assignments, route stop plans, vehicle clearance).

school_transport_route itself is not here: apps.schoollife already owns it."""

from .driver_assignment import AssignmentEventHandler, DriverAssignmentHandler
from .rider_assignment import RiderAssignmentEventHandler, RiderAssignmentHandler
from .route_plan import RouteEventHandler, RoutePlanHandler
from .vehicle_clearance import VehicleClearanceHandler, VehicleEventHandler

HANDLERS = [
    DriverAssignmentHandler(),
    AssignmentEventHandler(),
    RiderAssignmentHandler(),
    RiderAssignmentEventHandler(),
    RoutePlanHandler(),
    RouteEventHandler(),
    VehicleClearanceHandler(),
    VehicleEventHandler(),
]
