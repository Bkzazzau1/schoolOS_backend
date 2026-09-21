import base64
import binascii
from typing import Any

from apps.core.errors import Rejected
from apps.core.validation import choice, integer
from apps.schools.models import Role
from apps.sync.registry import EntityHandler, MutationContext

from .constants import APPEARANCE, APPEARANCE_ID, CUSTOM_THEME, MAX_LOGO_BASE64, THEMES


class AppearanceHandler(EntityHandler):
    """The school's colour scheme and logo. The owner chooses them and everyone in
    the school receives them, so the whole school looks the same. The scheme is a
    ready-made one or the owner's own two colours. Who chose it and when are
    stamped by the server."""

    entity_type = APPEARANCE
    roles = frozenset({Role.PROPRIETOR})

    def visible(self, membership, payload):
        return payload

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        if ctx.entity_id != APPEARANCE_ID:
            raise Rejected("A school has one appearance record.")
        theme = choice(ctx.payload.get("themeId"), THEMES | {CUSTOM_THEME}, "themeId")
        cleaned: dict[str, Any] = {"themeId": theme}
        if theme == CUSTOM_THEME:
            cleaned["primaryArgb"] = integer(ctx.payload, "primaryArgb", minimum=0xFF000000, maximum=0xFFFFFFFF)
            cleaned["accentArgb"] = integer(ctx.payload, "accentArgb", minimum=0xFF000000, maximum=0xFFFFFFFF)
        logo = ctx.payload.get("logo")
        if logo:
            cleaned["logo"] = _clean_logo(logo)
        cleaned["updatedByMembershipId"] = str(ctx.membership.id)
        cleaned["updatedAt"] = ctx.now
        return cleaned


_PNG = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])
_JPEG = bytes([0xFF, 0xD8, 0xFF])


def _clean_logo(value: Any) -> str:
    """A logo is a small PNG or JPEG sent as base64 text. Anything else is refused."""
    if not isinstance(value, str):
        raise Rejected("logo must be a picture.")
    if len(value) > MAX_LOGO_BASE64:
        raise Rejected("The logo is too large. Choose a smaller picture.")
    try:
        data = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise Rejected("The logo is not a valid picture.") from None
    if not (data.startswith(_PNG) or data.startswith(_JPEG)):
        raise Rejected("The logo must be a PNG or JPEG picture.")
    return value
