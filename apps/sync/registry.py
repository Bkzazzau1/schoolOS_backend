"""Where features plug their rules into sync.

The app syncs generic records (an entity type plus a JSON payload). Sync itself
knows nothing about payroll or staff. Each feature owns the entity types it
cares about by registering an EntityHandler for them, which decides who may
change them and cleans what is stored.

    class SalaryProfileHandler(EntityHandler):
        entity_type = "owner_payroll_profile"
        roles = frozenset({"proprietor"})

        def clean(self, ctx):
            ...   # validate ctx.payload, return the dict to store

A feature registers its handlers in its AppConfig.ready(). To add a feature,
write handlers and register them; sync itself does not change.
"""

from dataclasses import dataclass
from typing import Any

from apps.core.errors import Rejected
from apps.schools.models import Membership


@dataclass(frozen=True)
class MutationContext:
    membership: Membership
    operation: str  # create | update | delete
    entity_type: str
    entity_id: str
    payload: dict[str, Any]
    #: The stored payload, or None when the record does not exist or was deleted.
    existing: dict[str, Any] | None
    #: The server's clock as an ISO string. Stamp times with this, never with
    #: a time the app sent.
    now: str


class EntityHandler:
    entity_type: str = ""
    roles: frozenset[str] = frozenset()
    allow_delete: bool = False

    def authorize(self, ctx: MutationContext) -> None:
        """Refuse the change before anything else is looked at."""
        if ctx.membership.role not in self.roles:
            raise Rejected("Your role may not change this kind of record.")
        if ctx.operation == "delete" and not self.allow_delete:
            raise Rejected("This kind of record cannot be deleted.")

    def visible(self, membership: Membership, payload: dict[str, Any]) -> dict[str, Any] | None:
        """What this person may download of a stored record: the payload to send
        (redact it here if part of it is private), or None if they may not see it.

        The default is the people who may change the record. Handlers whose records
        are also readable by others (the person a record is about, say) override it.
        """
        return payload if membership.role in self.roles else None

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        """Validate a create or update and return exactly what to store.

        Copy only the fields you know. Fields the server owns (who acted, when,
        links to accounts) must be set here, never taken from the app.
        """
        raise NotImplementedError

    def after_write(self, ctx: MutationContext, stored: dict[str, Any]) -> None:
        """Runs in the same transaction, right after a create or update is saved.

        Use it for anything that must stay consistent with the record, such as a
        table of unique values. Raise Rejected to undo the write: the record is
        rolled back and the app is told why.
        """


_handlers: dict[str, EntityHandler] = {}


def register(handler: EntityHandler) -> None:
    if not handler.entity_type:
        raise ValueError("A handler needs an entity_type.")
    if handler.entity_type in _handlers:
        raise ValueError(f"{handler.entity_type} already has a handler.")
    _handlers[handler.entity_type] = handler


def get(entity_type: str) -> EntityHandler | None:
    return _handlers.get(entity_type)


def registered_types() -> set[str]:
    return set(_handlers)
