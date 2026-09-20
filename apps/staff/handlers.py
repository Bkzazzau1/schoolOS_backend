"""Every sync handler the staff feature provides."""

from .directory.handler import StaffDirectoryHandler
from .profiles.handler import StaffProfileHandler
from .proposals.handler import StaffProposalHandler

HANDLERS = [
    StaffProposalHandler(),
    StaffDirectoryHandler(),
    StaffProfileHandler(),
]
