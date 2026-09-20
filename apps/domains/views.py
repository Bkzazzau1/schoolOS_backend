from django.conf import settings
from django.http import Http404, JsonResponse

from .models import SchoolDomain


def assetlinks(request):
    """/.well-known/assetlinks.json: what lets Android open links in the app.

    Served only on **platform** subdomains, because the app claims those (one
    wildcard entry covers every school). A school's own custom domain is not
    claimed by the app, so its links open the web page instead. It is also 404
    unless the app's package name and signing fingerprint are configured, so it
    can never vouch for an unknown app.
    """
    domain = getattr(request, "school_domain", None)
    package = getattr(settings, "ANDROID_APP_PACKAGE", "")
    fingerprints = list(getattr(settings, "ANDROID_CERT_SHA256", []))
    if domain is None or domain.kind != SchoolDomain.Kind.PLATFORM or not package or not fingerprints:
        raise Http404
    return JsonResponse(
        [
            {
                "relation": ["delegate_permission/common.handle_all_urls"],
                "target": {
                    "namespace": "android_app",
                    "package_name": package,
                    "sha256_cert_fingerprints": fingerprints,
                },
            }
        ],
        safe=False,
    )
