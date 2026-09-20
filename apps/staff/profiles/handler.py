from typing import Any

from apps.core.errors import Rejected
from apps.core.validation import choice, is_email, text
from apps.notifications.services import notify_many
from apps.schools.models import Membership
from apps.sync import records
from apps.sync.registry import EntityHandler, MutationContext

from .. import identity
from ..constants import DIRECTORY, EDITOR_ROLES, INVITE_PENDING, INVITER_ROLES, ONBOARDING_STATES, PROFILE, SUBMITTED
from ..signals import registration_requested
from . import rules, sections


class StaffProfileHandler(EntityHandler):
    """A staff member's full record: personal details, academic history,
    credentials, required documents, bank details, performance reviews, and their
    registration request.

    Who may change what is in rules.py. In short: the owner and principal edit
    everything except bank details; the administrator can only send the
    registration request; the staff member can fill in their own details, bank
    account and documents; nobody else can change anything.

    Bank details can be changed only by the staff member, and only once their login
    is linked to the record (which the server does when they accept their invitation).
    `linkedMembershipId` and `systemRole` are set by the server and are ignored if
    the app sends them. Reviews only ever grow and are stamped by the server.
    Phone numbers and NINs are unique to one person in the school.
    """

    entity_type = PROFILE
    roles = frozenset()  # anyone may try; what they may change is decided per section

    def authorize(self, ctx: MutationContext) -> None:
        if ctx.operation == "delete":
            raise Rejected("This kind of record cannot be deleted.")
        who = self._who(ctx)
        if ctx.membership.role not in INVITER_ROLES and not who.self_:
            raise Rejected("Your role may not change this kind of record.")

    def visible(self, membership, payload):
        """Owner, principal and the person themselves see everything, bank details
        included. The administrator reviews the file, so sees all but the bank
        account. Nobody else sees a staff record."""
        if membership.role in EDITOR_ROLES or payload.get("linkedMembershipId") == str(membership.id):
            return payload
        if membership.role == "administrator":
            return {**payload, "payment": {key: "" for key in sections.PAYMENT_KEYS}}
        return None

    @staticmethod
    def _who(ctx: MutationContext) -> rules.Who:
        linked = (ctx.existing or {}).get("linkedMembershipId") or ""
        return rules.Who(str(ctx.membership.id), ctx.membership.role, linked)

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        school, p = ctx.membership.school, ctx.payload
        if text(p, "staffId", max_len=128) != ctx.entity_id:
            raise Rejected("staffId must match the record.")
        if records.read(school, DIRECTORY, ctx.entity_id) is None:
            raise Rejected("Add the staff member first.")

        who = self._who(ctx)
        old = sections.loaded(ctx.existing)
        personal = sections.personal(p.get("personal"))
        academics = sections.academics(p.get("academics"))
        credentials = sections.credentials(p.get("credentials"))
        documents = sections.documents(p.get("documents"))
        payment = sections.payment(p.get("payment"))
        reviews = sections.reviews_shape(p.get("reviews"))
        status = choice(p.get("onboardingStatus", old["onboardingStatus"]), ONBOARDING_STATES, "onboardingStatus")
        email = text(p, "onboardingEmail", max_len=254, required=False).lower()
        if email and not is_email(email):
            raise Rejected("Enter a valid email address.")

        rules.check_personal(old["personal"], personal, who)
        rules.check_editor_only(old["academics"], academics, who, "academic records")
        rules.check_editor_only(old["credentials"], credentials, who, "credentials")
        rules.check_payment(old["payment"], payment, who)
        rules.check_documents(old["documents"], documents, who, inviting=status == INVITE_PENDING)
        stamped_reviews = rules.check_reviews(old["reviews"], reviews, who, now=ctx.now)
        rules.check_onboarding(
            old["onboardingStatus"], old["onboardingEmail"], status, email, who, personal, payment
        )
        identity.check_available(
            school, "staff", ctx.entity_id, phone=personal["phone"] or None, nin=personal["nin"] or None
        )

        stored = {
            "staffId": ctx.entity_id,
            "personal": personal,
            "academics": academics,
            "credentials": credentials,
            "reviews": stamped_reviews,
            "payment": payment,
            "documents": documents,
            "onboardingStatus": status,
            "onboardingEmail": email,
            # Set by the server, never by the app:
            "linkedMembershipId": old["linkedMembershipId"],
            "systemRole": old["systemRole"],
            "updatedAt": ctx.now,
            "updatedByMembershipId": str(ctx.membership.id),
        }
        # When they first submitted. Set by the server the moment the status becomes
        # submitted, and kept on every later save.
        submitted_at = (ctx.existing or {}).get("submittedAt")
        if status == SUBMITTED and old["onboardingStatus"] != SUBMITTED:
            submitted_at = ctx.now
        if submitted_at:
            stored["submittedAt"] = submitted_at
        return stored

    def after_write(self, ctx: MutationContext, stored: dict[str, Any]) -> None:
        school = ctx.membership.school
        directory = records.read(school, DIRECTORY, ctx.entity_id) or {}
        identity.set_claims(
            school, "staff", ctx.entity_id, directory.get("name", ctx.entity_id),
            phone=stored["personal"]["phone"] or None, nin=stored["personal"]["nin"] or None,
        )
        old = ctx.existing or {}
        was = old.get("onboardingStatus")
        # A registration request was sent (or its email corrected): tell the
        # invitations feature so it emails the link.
        if stored["onboardingStatus"] == INVITE_PENDING and (
            was != INVITE_PENDING or old.get("onboardingEmail") != stored["onboardingEmail"]
        ):
            registration_requested.send(
                sender=None, school=school, staff_id=ctx.entity_id, email=stored["onboardingEmail"],
                system_role=stored.get("systemRole") or "", name=directory.get("name", ""),
                requested_by=ctx.membership,
            )
        if stored["onboardingStatus"] == SUBMITTED and was != SUBMITTED:
            reviewers = Membership.objects.filter(
                school=school, role__in=["proprietor", "principal"], is_active=True
            ).select_related("school")
            notify_many(
                reviewers, "staff_registration_submitted", "Staff registration submitted",
                f"{directory.get('name', 'A staff member')} has submitted their registration for review.",
                {"staffId": ctx.entity_id},
            )
