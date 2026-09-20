"""One shared handler for the school-life modules, driven by a small spec per module.

Most modules are the same shape: some people manage the records, some can add their
own, everyone (or a narrower group) reads them, and a few fields are the leadership's
to decide (a review tick, a public-showcase setting). A module is therefore a `Spec`
(see specs/), not a class, so adding or changing a module is a few lines.

What the handler always does, whatever the module:
- only the roles named in the spec may write; contributors may only change what they added;
- the record is an object of reasonable size, and its id field matches the record id;
- required fields are present, and no text field is huge;
- server-owned fields (counters) keep the server's value, whatever a device sends;
- guarded fields (review ticks, public visibility) can only be changed by their roles, and start at their default;
- who created it, who last changed it and when are set by the server, never taken from the app;
- records are not deleted unless the spec says so, and only by managers.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from apps.core.errors import Rejected
from apps.core.validation import text
from apps.schools.models import Membership
from apps.sync.registry import EntityHandler, MutationContext

MANAGERS = frozenset({"proprietor", "principal", "administrator"})
LEADERS = frozenset({"proprietor", "principal"})
STAFF_SIDE = MANAGERS | {"accountant", "teacher", "staff"}
EVERYONE = STAFF_SIDE | {"parent", "student"}

MAX_BYTES = 60_000
MAX_TEXT = 5_000
SERVER_FIELDS = ("createdByMembershipId", "createdAt", "updatedByMembershipId", "updatedAt")


@dataclass(frozen=True)
class Spec:
    entity_type: str
    #: May create, change and (if allowed) delete anything in the module.
    manage: frozenset = MANAGERS
    #: May create records, and change only their own.
    contribute: frozenset = frozenset()
    #: Who receives the records. Managers and the person who added a record always do.
    read: frozenset = EVERYONE
    #: The payload key that must equal the record id (None if the id is derived).
    id_field: str | None = "id"
    #: Fields that must be present and non-empty text.
    required: tuple = ()
    #: field -> roles allowed to change it. The starting value is `defaults[field]`.
    guarded: dict = field(default_factory=dict)
    #: (field, value) -> roles allowed to set that value.
    guarded_values: dict = field(default_factory=dict)
    defaults: dict = field(default_factory=dict)
    #: field -> starting value. The server keeps these (a counter, say); whatever a device sends is replaced.
    server_owned: dict = field(default_factory=dict)
    #: Given a stored payload, the roles that may see it (None = every role in `read`).
    audience: Callable[[dict], frozenset | None] = lambda payload: None
    allow_delete: bool = False


class SchoolLifeHandler(EntityHandler):
    def __init__(self, spec: Spec):
        self.spec = spec
        self.entity_type = spec.entity_type
        self.roles = spec.manage | spec.contribute

    # -- who may write --------------------------------------------------------------

    def authorize(self, ctx: MutationContext) -> None:
        spec, role = self.spec, ctx.membership.role
        if role not in self.roles:
            raise Rejected("Your role may not change this kind of record.")
        if ctx.operation == "delete" and (not spec.allow_delete or role not in spec.manage):
            raise Rejected("This kind of record cannot be deleted.")
        if ctx.operation == "update" and role not in spec.manage and not self._is_own(ctx.existing, ctx.membership):
            raise Rejected("You can only change what you added yourself.")

    @staticmethod
    def _is_own(payload: dict | None, membership: Membership) -> bool:
        return bool(payload) and payload.get("createdByMembershipId") == str(membership.id)

    # -- what is stored -----------------------------------------------------------------

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        spec, p, old = self.spec, dict(ctx.payload), ctx.existing
        if len(json.dumps(p)) > MAX_BYTES:
            raise Rejected("This record is too large.")
        if any(isinstance(v, str) and len(v) > MAX_TEXT for v in p.values()):
            raise Rejected("A text field is too long.")
        if spec.id_field and str(p.get(spec.id_field, "")).strip().lower() != ctx.entity_id.lower():
            raise Rejected(f"{spec.id_field} must match the record.")
        for name in spec.required:
            text(p, name, max_len=MAX_TEXT)
        for name in SERVER_FIELDS:
            p.pop(name, None)

        for name, start in spec.server_owned.items():
            p[name] = (old or {}).get(name, start)

        role = ctx.membership.role
        for name, roles in spec.guarded.items():
            if old is None and name not in spec.defaults:
                continue  # no starting value is required when a record is first added
            before = (old or {}).get(name, spec.defaults.get(name))
            if p.get(name, before) != before and role not in roles:
                raise Rejected(f"Only {self._who(roles)} can change {name}.")
            if name not in p and before is not None:
                p[name] = before
        for (name, value), roles in spec.guarded_values.items():
            before = (old or {}).get(name)
            if p.get(name) == value and before != value and role not in roles:
                raise Rejected(f"Only {self._who(roles)} can set {name} to {value}.")

        p["createdByMembershipId"] = (old or {}).get("createdByMembershipId") or str(ctx.membership.id)
        p["createdAt"] = (old or {}).get("createdAt") or ctx.now
        p["updatedByMembershipId"] = str(ctx.membership.id)
        p["updatedAt"] = ctx.now
        return p

    @staticmethod
    def _who(roles) -> str:
        return " or ".join(sorted(r.replace("proprietor", "owner") for r in roles))

    # -- who receives it -----------------------------------------------------------------

    def visible(self, membership, payload):
        spec = self.spec
        if membership.role in spec.manage or self._is_own(payload, membership):
            return payload
        if membership.role not in spec.read:
            return None
        allowed = spec.audience(payload)
        return payload if allowed is None or membership.role in allowed else None
