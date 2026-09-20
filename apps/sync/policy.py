"""Who may write which kind of record.

This is the server-side rule set. The app enforces the same rules on the
device, but a device can be modified, so the server must not rely on that.

Only entity types listed here are trusted. Anything else is refused in
production (see SYNC_ALLOW_UNLISTED_ENTITY_TYPES) until someone writes its
rules.

Many rules cannot be expressed as "this role may write this type", because
the app's workflows are finer than that. Examples still to be built as server
handlers before their types are listed:

  * staff_proposal      any of several roles may propose, only the owner or an
                        assigned approver may change its status;
  * owner_staff_profile only the linked staff member may change bank details,
                        while the owner and principal edit the rest;
  * payroll_batch       prepare, approve and release are separate authorities,
                        and the approver cannot be the preparer;
  * staff onboarding    only the login linked to the staff record may submit.

Until those handlers exist those types stay unlisted, which means refused in
production. That is deliberate: refusing is safe, guessing is not.
"""

from dataclasses import dataclass

from django.conf import settings

from apps.schools.models import Role


@dataclass(frozen=True)
class EntityPolicy:
    roles: frozenset[str]


ENTITY_POLICIES: dict[str, EntityPolicy] = {
    # Owner-only records: salaries, payroll authority and job assignments.
    "owner_payroll_profile": EntityPolicy(frozenset({Role.PROPRIETOR})),
    "owner_payroll_authorizer": EntityPolicy(frozenset({Role.PROPRIETOR})),
    "owner_job_assignment": EntityPolicy(frozenset({Role.PROPRIETOR})),
}


def is_allowed(role: str, entity_type: str) -> bool:
    policy = ENTITY_POLICIES.get(entity_type)
    if policy is None:
        return bool(settings.SYNC_ALLOW_UNLISTED_ENTITY_TYPES)
    return role in policy.roles
