from .services import resolve_host


class SchoolHostMiddleware:
    """Works out which school a web request is for, from its Host header.

    Sets `request.school_domain` and `request.school` (both None when the host is
    not a verified school domain). It only identifies; it does not refuse
    anything, so the API keeps working on the platform's own host.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        host = request.META.get("HTTP_HOST", "")
        # Drop a port. Forwarded-host headers are deliberately not trusted.
        host = host.rsplit(":", 1)[0] if ":" in host and not host.endswith("]") else host
        domain = resolve_host(host) if host else None
        request.school_domain = domain
        request.school = domain.school if domain else None
        return self.get_response(request)
