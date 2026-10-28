"""Every sync handler the structure feature provides."""

from .appearance import AppearanceHandler
from .appointments import AppointmentHandler
from .campuses import CampusHandler
from .sections import SectionHandler

HANDLERS = [CampusHandler(), SectionHandler(), AppointmentHandler(), AppearanceHandler()]
