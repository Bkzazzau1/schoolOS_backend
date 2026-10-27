import hashlib

from rest_framework.throttling import AnonRateThrottle


class RecoveryIdentifierThrottle(AnonRateThrottle):
    """Rate-limit recovery requests per opaque login identifier."""

    rate = "5/hour"

    def get_cache_key(self, request, view):
        getter = getattr(request.data, "get", None)
        identifier = str(getter("identifier") or "").strip().casefold() if getter else ""
        if not identifier:
            return None
        digest = hashlib.sha256(identifier.encode("utf-8")).hexdigest()
        return self.cache_format % {
            "scope": "credential_recovery_id",
            "ident": digest,
        }


class RecoveryNetworkThrottle(AnonRateThrottle):
    """A broad abuse ceiling that still permits shared school networks."""

    rate = "120/hour"
    scope = "credential_recovery_network"


def _email_cache_key(throttle, request, scope):
    getter = getattr(request.data, "get", None)
    email = str(getter("email") or "").strip().casefold() if getter else ""
    if not email:
        return None
    digest = hashlib.sha256(email.encode("utf-8")).hexdigest()
    return throttle.cache_format % {"scope": scope, "ident": digest}


class PasswordResetRequestThrottle(AnonRateThrottle):
    """Bound how often a reset code is sent to one email-login account."""

    rate = "5/hour"

    def get_cache_key(self, request, view):
        return _email_cache_key(self, request, "password_reset_request")


class PasswordResetConfirmThrottle(AnonRateThrottle):
    """Bound online code-guessing independently of the per-code attempt counter."""

    rate = "10/min"

    def get_cache_key(self, request, view):
        return _email_cache_key(self, request, "password_reset_confirm")
