"""Every school-life module, as a list of specs. Add a module by adding its spec to one of these files."""

from . import calendar, campus, communications, programmes

SPECS = [*communications.SPECS, *calendar.SPECS, *programmes.SPECS, *campus.SPECS]
