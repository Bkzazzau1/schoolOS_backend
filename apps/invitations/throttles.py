from rest_framework.throttling import SimpleRateThrottle


class _PerAddress(SimpleRateThrottle):
    """Limits by the caller's address, whether or not they are signed in."""

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


class PreviewThrottle(_PerAddress):
    scope = "invite_preview"


class AcceptThrottle(_PerAddress):
    scope = "invite_accept"
