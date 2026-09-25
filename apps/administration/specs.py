"""The Administrator's own front-desk modules: attendance, notices, document records, staff attendance.

Every one of these was already being written by the app and queued for sync - there was simply no
handler on this end to receive them, so they sat pending forever. They are Administrator-only writes
today (the app's own permissionsFor() never lets any other role act on them), so each spec keeps that
shape exactly rather than widening it to the broader MANAGERS tier used elsewhere. Reads go to school
leadership (Proprietor, Principal, Administrator) for oversight, since none of these are yet read by
any other role's screen.

Reuses the same generic Spec/SchoolLifeHandler engine schoollife's modules use - the engine has no
schoollife-specific logic in it, so there is nothing to duplicate.
"""

from apps.schoollife.framework import MANAGERS, Spec

_ADMINISTRATOR = frozenset({"administrator"})

ATTENDANCE_EVENTS = Spec(
    "administrator_attendance_event",
    manage=_ADMINISTRATOR,
    read=MANAGERS,
    # entityKey can be empty on a brand-new check-in (the app derives the real
    # storage id from time+student instead), so the payload cannot be required
    # to redundantly carry a matching id.
    id_field=None,
    required=("time", "student", "date"),
)

ATTENDANCE_CORRECTIONS = Spec(
    "administrator_attendance_correction",
    manage=_ADMINISTRATOR,
    read=MANAGERS,
    required=("id", "student", "requestedChange"),
)

ATTENDANCE_DEVICES = Spec(
    "administrator_attendance_device",
    manage=_ADMINISTRATOR,
    read=MANAGERS,
    # A device's storage id is its display name today ("Main Gate Face
    # Terminal"), which is not itself a safe sync id, so the payload is not
    # required to redundantly carry a matching id field.
    id_field=None,
    required=("name",),
)

NOTICES = Spec(
    "administrator_notice",
    manage=_ADMINISTRATOR,
    read=MANAGERS,
    required=("id", "title", "message"),
)

DOCUMENT_RECORDS = Spec(
    "administrator_document_record",
    manage=_ADMINISTRATOR,
    read=MANAGERS,
    required=("id", "document", "recordOwner", "kind"),
)

STAFF_ATTENDANCE = Spec(
    "administrator_staff_attendance",
    manage=_ADMINISTRATOR,
    read=MANAGERS,
    required=("id", "name", "role"),
)

PAYROLL_ATTENDANCE_SUMMARY = Spec(
    "payroll_attendance_summary",
    manage=_ADMINISTRATOR,
    read=MANAGERS,
    required=("id",),
)

# No write action reaches this yet - the app only ever seeds it locally and
# displays it, with nothing to press that would queue a mutation. Registered
# now so the day a real "add operations task" action ships, no backend work
# is needed for it. Its storage id is synthetic (operations-1, operations-2,
# ...), not one of the task's own fields, so the payload cannot be required
# to redundantly carry a matching id.
OPERATIONS_QUEUE = Spec(
    "administrator_operations_queue",
    manage=_ADMINISTRATOR,
    read=MANAGERS,
    id_field=None,
    required=("title",),
)

SPECS = [
    ATTENDANCE_EVENTS,
    ATTENDANCE_CORRECTIONS,
    ATTENDANCE_DEVICES,
    NOTICES,
    DOCUMENT_RECORDS,
    STAFF_ATTENDANCE,
    PAYROLL_ATTENDANCE_SUMMARY,
    OPERATIONS_QUEUE,
]
