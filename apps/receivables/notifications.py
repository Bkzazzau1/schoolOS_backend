"""Telling people about what happens to a family's account, through the same in-app notifications the
rest of SchoolOS uses. Nobody is told everything: the finance side hears about what needs them (fees
published, an overpayment held as credit, a collection account waking up), and a family hears about its
own account (new fees, a payment received, fees settled).
"""

from apps.bankconnect.permissions import collections_recipients
from apps.core.money import format_money
from apps.notifications.services import notify_many
from apps.schools.models import Membership, Role
from apps.students.models import GuardianLink

from . import ledger
from .permissions import billing_authorities

KIND_FEES_PUBLISHED = "fees_published"
KIND_NEW_FEES = "family_new_fees"
KIND_PAYMENT = "family_payment"
KIND_SETTLED = "family_fees_settled"
KIND_CREDIT = "family_credit"
KIND_ACCOUNT_ACTIVE = "collection_account_active"
SANDBOX_PREFIX = "[Sandbox test data] "


def finance_recipients(school) -> list:
    """The owner, the finance office, and anyone the owner gave billing or bank authority - each once."""
    seen: dict = {}
    for member in [*collections_recipients(school), *billing_authorities(school)]:
        seen[member.id] = member
    return list(seen.values())


def parent_recipients(family) -> list:
    """The signed-in parent accounts of the guardians of the family's students."""
    users = GuardianLink.objects.filter(
        student__family_memberships__family=family, student__family_memberships__is_active=True, account_user__isnull=False,
    ).values_list("account_user_id", flat=True)
    return list(Membership.objects.select_related("school").filter(school=family.school, role=Role.PARENT, is_active=True, user_id__in=set(users)))


def _tell(recipients, kind, title, message, data, prefix=""):
    if recipients:
        notify_many(recipients, kind, prefix + title, prefix + message, data)


def schedule_published(schedule, report, families) -> None:
    school = schedule.school
    parts = [f"{report.created} charge(s) raised for {len(families)} family(ies)."]
    if report.without_family:
        parts.append(f"{len(report.without_family)} student(s) have no family yet and were not charged.")
    if report.unclassified:
        parts.append(f"{len(report.unclassified)} student(s) have no class for the session and were not charged.")
    _tell(
        finance_recipients(school), KIND_FEES_PUBLISHED, f"Fees published: {schedule.name}", " ".join(parts),
        {"scheduleId": str(schedule.id), "charges": report.created},
    )
    for family in families:
        owed = ledger.family_position(family).outstanding
        _tell(
            parent_recipients(family), KIND_NEW_FEES, "New fees",
            f"New fees ({schedule.name}) have been published for {family.display_name}. {format_money(owed)} is now due.",
            {"familyId": str(family.id), "scheduleId": str(schedule.id)},
        )


def after_settlement(family, after, *, payment=None, account_changes=()) -> None:
    school = family.school
    if payment is not None:
        tx, result, before = payment.transaction, payment.result, payment.before
        prefix = SANDBOX_PREFIX if tx.is_sandbox else ""
        received = format_money(result.total_minor, tx.currency)
        if before.outstanding > 0 and after.outstanding == 0:
            title, message, kind = "Fees settled", f"Thank you: {family.display_name} has now paid everything due. We received {received}.", KIND_SETTLED
        elif result.allocated_minor > 0:
            title, kind = "Payment received", KIND_PAYMENT
            message = f"We received {received} for {family.display_name}. {format_money(after.outstanding)} is still due."
        else:
            title, kind = "Payment received", KIND_PAYMENT
            message = f"We received {received} for {family.display_name}. Nothing was due, so it is held as credit."
        _tell(parent_recipients(family), kind, title, message, {"familyId": str(family.id), "transactionId": str(tx.id)}, prefix)
        if result.credit_minor > 0:
            _tell(
                finance_recipients(school), KIND_CREDIT, "Overpayment held as credit",
                f"{format_money(result.credit_minor, tx.currency)} from {family.display_name} was more than they owed and is held as family credit.",
                {"familyId": str(family.id), "transactionId": str(tx.id)}, prefix,
            )
    for account, was, now in account_changes:
        if was == "dormant" and now == "active":
            _tell(
                finance_recipients(school), KIND_ACCOUNT_ACTIVE, "Collection account active again",
                f"{family.display_name} owes money again, so its collection account is active.", {"familyId": str(family.id)},
            )
