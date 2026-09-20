from dataclasses import dataclass

from apps.schools.models import Role


@dataclass(frozen=True)
class Activity:
    """One thing in the app a person can be allowed to see.

    `key` is `<workspace>.<screen>` and the screen part is the same key the app
    already uses for that screen in its navigation, so the app can map one to the
    other without a lookup table.
    """

    key: str
    label: str
    #: The part of the app it belongs to, for grouping in the owner's screen.
    area: str
    #: Roles that have it by default. Empty means nobody does until the owner
    #: grants it.
    default_roles: frozenset[str]
    #: Cannot be taken away from a role or a person, because losing it would lock
    #: them out of the app (a landing screen).
    essential: bool = False
    #: False means the owner cannot give it to anyone outside its default roles.
    grantable: bool = True
    #: Shows money or personal data. Informational: the owner's screen warns
    #: before granting it.
    sensitive: bool = False


def workspace(
    prefix: str,
    area: str,
    roles: set[str],
    items: list[tuple],
) -> list[Activity]:
    """Build a workspace's activities from (screen key, label, flags...) rows.

    A flag is one of "essential", "sensitive" or "owner-only" (not grantable).
    """
    built = []
    for key, label, *flags in items:
        built.append(
            Activity(
                key=f"{prefix}.{key}",
                label=label,
                area=area,
                default_roles=frozenset(roles),
                essential="essential" in flags,
                grantable="owner-only" not in flags,
                sensitive="sensitive" in flags,
            )
        )
    return built


PROPRIETOR = Role.PROPRIETOR.value
