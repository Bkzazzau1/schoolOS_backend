"""Every sync handler the owner feature provides, in one list.

Add a new owner record type by writing its handler in its area's folder and
adding it here. OWNER_ENTITY_TYPES then covers it in the read endpoint too.
"""

from .jobs.assignments import JobAssignmentHandler
from .payroll.authorizers import AuthorizerHandler
from .payroll.salary import SalaryProfileHandler

HANDLERS = [
    SalaryProfileHandler(),
    AuthorizerHandler(),
    JobAssignmentHandler(),
]

OWNER_ENTITY_TYPES = {handler.entity_type for handler in HANDLERS}
