"""The owner's "needs your attention" list: things only people can move forward.

Each item says what, how many, how urgent, and which screen deals with it. An empty
list means nothing is waiting.
"""

from django.utils import timezone

from apps.invitations.models import StaffInvitation
from apps.owner.jobs.access import JOB
from apps.owner.payroll.access import AUTHORIZER
from apps.staff.constants import PENDING, PROPOSAL

from . import concessions, payroll, staff, structure
from .data import payloads

HIGH, MEDIUM, INFO = "high", "medium", "info"


def _item(key, severity, title, count, screen):
    return {"key": key, "severity": severity, "title": title, "count": count, "screen": screen}


def _plural(n, one, many):
    return f"{n} {one if n == 1 else many}"


def build(school) -> list[dict]:
    items = []

    proposals = [p for p in payloads(school, PROPOSAL) if p.get("status") == PENDING]
    if proposals:
        items.append(_item("staff_proposals", HIGH, f"{_plural(len(proposals), 'staff proposal', 'staff proposals')} to decide",
                           len(proposals), "owner.staff-profiles"))

    pay = payroll.summary(school)
    if pay["waitingForApproval"]:
        n = len(pay["waitingForApproval"])
        items.append(_item("payroll_approval", HIGH, f"{_plural(n, 'payroll batch', 'payroll batches')} waiting for approval", n, "owner.payroll"))
    if pay["waitingForPayment"]:
        n = len(pay["waitingForPayment"])
        items.append(_item("payroll_payment", MEDIUM, f"{_plural(n, 'approved batch', 'approved batches')} waiting for payment", n, "owner.payroll"))

    con = concessions.summary(school)["pending"]
    if con["count"]:
        items.append(_item("concessions", MEDIUM, f"{_plural(con['count'], 'scholarship or discount request', 'scholarship and discount requests')} to decide",
                           con["count"], "owner.finance-approvals"))

    now = timezone.now()
    invitations = StaffInvitation.objects.filter(school=school, status=StaffInvitation.Status.PENDING)
    stuck = [i for i in invitations if i.expires_at <= now or i.sent_at is None]
    if stuck:
        items.append(_item("invitations", MEDIUM, f"{_plural(len(stuck), 'invitation', 'invitations')} expired or not delivered",
                           len(stuck), "owner.staff-profiles"))

    people = staff.summary(school)
    waiting = people["registration"]["waitingForReview"]
    if waiting:
        items.append(_item("registrations", MEDIUM, f"{_plural(waiting, 'staff registration', 'staff registrations')} to review", waiting, "owner.staff-profiles"))
    if people["filesMissingDocuments"]:
        n = people["filesMissingDocuments"]
        items.append(_item("missing_documents", INFO, f"{_plural(n, 'staff file', 'staff files')} missing documents", n, "owner.staff-profiles"))

    unclaimed = sum(
        1 for kind in (AUTHORIZER, JOB) for r in payloads(school, kind) if r.get("status") == "pendingActivation"
    )
    if unclaimed:
        items.append(_item("unclaimed_access", INFO, f"{_plural(unclaimed, 'assignment', 'assignments')} not active yet: the person has no login",
                           unclaimed, "owner.jobs"))

    headless = [s for s in structure.summary(school)["sections"] if s["head"] is None]
    if headless:
        items.append(_item("sections_without_head", INFO, f"{_plural(len(headless), 'section', 'sections')} without a head",
                           len(headless), "owner.structure"))

    order = {HIGH: 0, MEDIUM: 1, INFO: 2}
    return sorted(items, key=lambda i: order[i["severity"]])
