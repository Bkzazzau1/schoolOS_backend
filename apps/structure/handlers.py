"""Every sync handler the structure feature provides."""

from .appearance import AppearanceHandler
from .appointments import AppointmentHandler
from .sections import SectionHandler

HANDLERS = [SectionHandler(), AppointmentHandler(), AppearanceHandler()]
