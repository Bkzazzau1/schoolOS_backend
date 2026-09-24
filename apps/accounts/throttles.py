import hashlib

from rest_framework.throttling import AnonRateThrottle, SimpleRateThrottle, UserRateThrottle

from .identity import normalize_parent_phone, normalize_student_admission


class LoginIdentifierThrottle(SimpleRateThrottle):
    """Bound password guessing per SchoolOS login identity.

    Schools commonly place a whole lab behind one public IP. Keying the main
    brute-force limit by a hashed normalized login ID protects each account
    without blocking a class of pupils who legitimately sign in together.
    """

    rate = "10/min"

    def get_cache_key(self, request, view):
        raw = str(
            request.data.get("identifier")
            or request.data.get("email")
            or ""
        ).strip()
        phone = normalize_parent_phone(raw)
        if phone:
            normalized = f"phone:{phone}"
        elif "@" in raw:
            normalized = f"email:{raw.casefold()}"
        else:
            normalized = f"admission:{normalize_student_admission(raw)}"

        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        return self.cache_format % {
            "scope": "login_identifier",
            "ident": digest,
        }


class LoginNetworkThrottle(AnonRateThrottle):
    """A wider abuse ceiling for one public network without penalising labs."""

    rate = "120/min"


class RegistrationThrottle(AnonRateThrottle):
    """Keep public account creation bounded independently of other anonymous API use."""

    rate = "5/min"


class EmailVerificationSendThrottle(UserRateThrottle):
    """Prevent verification-email spam from an authenticated account."""

    rate = "3/hour"


class EmailVerificationConfirmThrottle(UserRateThrottle):
    """Bound online guessing independently of the per-code attempt counter."""

    rate = "10/min"
