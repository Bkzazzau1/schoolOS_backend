"""Every sync handler the transport feature provides.

Phase 1: Transport Control's own policy records - driver and rider assignments, route
stop plans, vehicle clearance. Phase 2: a Driver's own daily runs and vehicle checks.

school_transport_route itself is not here: apps.schoollife already owns it."""

from .afternoon_run import AfternoonRunHandler
from .driver_assignment import AssignmentEventHandler, DriverAssignmentHandler
from .morning_run import MorningRunHandler
from .rider_assignment import RiderAssignmentEventHandler, RiderAssignmentHandler
from .route_plan import RouteEventHandler, RoutePlanHandler
from .transport_event import TransportEventHandler
from .vehicle_check import VehicleCheckHandler, VehicleDefectHandler
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
    MorningRunHandler(),
    AfternoonRunHandler(),
    VehicleCheckHandler(),
    VehicleDefectHandler(),
    TransportEventHandler(),
]
