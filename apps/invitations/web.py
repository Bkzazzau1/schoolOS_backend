"""The web pages for someone who opens their invitation without the app.

  /invite/<link>/         what the link is for; set a password (or sign in) to accept
  /invite/registration/   then fill in the registration form, the same as in the app

Both are plain server-rendered pages. The registration form calls the very same
function the app's sync path uses (apps/staff/registration.py), so the rules are
identical. The pages are never cached, and never send the link on as a referrer.
"""

from django.contrib.auth import authenticate, login
from django.core.cache import cache
from django.http import HttpResponse
from django.shortcuts import render
from django.views.decorators.cache import never_cache

from apps.core.errors import Rejected
from apps.staff import identity, registration
from apps.staff.profiles.sections import PERSONAL_KEYS

from . import accept as accepting
from .service import InvitationError
from .tokens import hash_token

MAX_ATTEMPTS = 10          # wrong passwords or bad forms, per link and address
ATTEMPT_WINDOW = 15 * 60   # seconds
BACKEND = "django.contrib.auth.backends.ModelBackend"


def _page(request, template: str, context: dict | None = None, status: int = 200) -> HttpResponse:
    response = render(request, f"invitations/{template}.html", context or {}, status=status)
    response["Referrer-Policy"] = "no-referrer"  # never pass the link on to another site
    return response


def _too_many_attempts(request, token: str) -> bool:
    key = f"invite-web:{request.META.get('REMOTE_ADDR', '')}:{hash_token(token)[:16]}"
    cache.add(key, 0, ATTEMPT_WINDOW)
    try:
        return cache.incr(key) > MAX_ATTEMPTS
    except ValueError:
        return False


@never_cache
def invite_page(request, token):
    school = getattr(request, "school", None)
    try:
        info = accepting.preview(token, request_school=school, request=request if request.method == "GET" else None)
    except InvitationError as error:
        page = "already_used" if error.code == "already_accepted" else "invalid"
        return _page(request, page, status=error.status)
    context = {"info": info, "token": token, "error": ""}
    if request.method != "POST":
        return _page(request, "invite", context)

    if _too_many_attempts(request, token):
        return _page(request, "invalid", {"message": "Too many attempts. Please wait a while and try again."}, status=429)
    password = request.POST.get("password", "")
    signed_in = None
    try:
        if info["accountExists"]:
            invitation = accepting.find(token, request_school=school)
            signed_in = authenticate(request, username=invitation.email, password=password)
            if signed_in is None:
                raise InvitationError("invalid_password", "That password is not correct.", 400)
        elif password != request.POST.get("password2", ""):
            raise InvitationError("invalid_password", "The two passwords do not match.", 400)
        result = accepting.accept(
            token, signed_in_user=signed_in, password=password,
            first_name=request.POST.get("first_name", ""), last_name=request.POST.get("last_name", ""),
            request_school=school, request=request,
        )
    except InvitationError as error:
        details = error.extra.get("details")
        context["error"] = " ".join(details) if details else error.message
        return _page(request, "invite", context, status=error.status if error.status != 401 else 400)
    login(request, result["user"], backend=BACKEND)
    from django.shortcuts import redirect

    return redirect("/invite/registration/")


@never_cache
def registration_page(request):
    if not request.user.is_authenticated:
        return _page(request, "invalid", {"message": "Please open the link from your invitation email again."}, status=403)
    found = registration.find_open_request(request.user)
    if found is None:
        return _page(request, "done", {"nothing": True})
    membership, record = found
    payload = record.payload
    docs = [d for d in payload.get("documents", []) if d.get("status") == "requested"]
    context = {"school": membership.school.name, "values": {**payload.get("personal", {}), **payload.get("payment", {})},
               "docs": [{"index": i, "name": d["name"]} for i, d in enumerate(docs)], "error": ""}
    if request.method != "POST":
        return _page(request, "registration", context)

    post = request.POST
    personal = {key: post.get(key, "") for key in PERSONAL_KEYS if key in post}
    payment = {key: post.get(key, "") for key in ("bankName", "accountName", "accountNumber")}
    notes = {d["name"]: post.get(f"doc_{i}", "") for i, d in enumerate(docs)}
    context["values"] = {**context["values"], **personal, **payment}
    try:
        registration.submit(membership, personal=personal, payment=payment, documents=notes)
    except identity.Duplicate as duplicate:
        context["error"] = duplicate.message + " If this is you, contact the school office."
        return _page(request, "registration", context, status=409)
    except Rejected as rejected:
        context["error"] = rejected.message
        return _page(request, "registration", context, status=400)
    return _page(request, "done", {"school": membership.school.name})
