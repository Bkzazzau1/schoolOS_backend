"""The payer's consent to a mandate. It is never given by a member of staff: it is the payer themselves, signed in as themselves, reviewing the exact
words below (`payer_app`), or the provider's own authorisation - a bank one-time password, a signed form, an activation transfer from the payer's
account - which SchoolOS keeps a reference to (`provider_hosted`).

The wording is versioned and its hash is stored with the consent, so what the payer agreed to can be proved later. Its exact legal wording, and any
notice a payer must be given before a debit, are for the school and its advisers: SchoolOS has not verified either, and the words here say only what
the mandate is.
"""

import hashlib

from django.db import transaction
from django.utils import timezone

from .constants import ConsentChannel
from .errors import MandateRefused
from .models import MandateConsent
from .providers import registry

CONSENT_VERSION = "1"


def _maximum_words(mandate) -> str:
    connector = registry.get_connector(mandate.provider)
    scope = connector.info.maximum_note if connector else ""
    if mandate.maximum_amount_minor is None:
        return "the school will only debit what it has approved for the fees I owe"
    naira = f"NGN {mandate.maximum_amount_minor / 100:,.2f}"
    return f"the most that can be debited is {naira} ({scope.lower().rstrip('.')})" if scope else f"the most that can be debited is {naira}"


def consent_text(mandate) -> str:
    """The exact words the payer is shown and agrees to. Built from the mandate, so it is the same wherever it is shown."""
    connector = registry.get_connector(mandate.provider)
    provider = connector.info.display_name if connector else mandate.provider
    start = mandate.start_date.strftime("%d %B %Y") if mandate.start_date else "the day it is activated"
    end = mandate.end_date.strftime("%d %B %Y") if mandate.end_date else "until it is cancelled"
    return (
        f"I, {mandate.payer.guardian.name}, authorise {mandate.school.name} to collect the school fees that I owe for {mandate.family.display_name} by "
        f"direct debit from my {mandate.bank_name or 'bank'} account ending {mandate.account_mask[-4:]}, through {provider}. "
        f"Each debit is for an amount the school has approved and that does not exceed what is owed in school fees; {_maximum_words(mandate)}. "
        f"This authority runs from {start} to {end}. I can cancel it at any time, and the school will not debit my account until my bank has activated it."
    )


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _summarise(mandate, consent: MandateConsent) -> None:
    mandate.consent_at, mandate.consent_channel = consent.consented_at, consent.channel
    mandate.consent_version, mandate.consent_reference = consent.consent_version, consent.provider_consent_reference


def record_payer_consent(mandate, membership, *, shown_hash: str) -> MandateConsent:
    """The payer, signed in as themselves, authorises the mandate. `shown_hash` is the hash of the words their screen showed: if the mandate has
    changed since, it no longer matches and they must look again. The caller holds the mandate's row and saves it."""
    if not membership.user_id or mandate.payer.guardian.account_user_id != membership.user_id:
        raise MandateRefused("Only the payer named on this mandate can authorise it.", "not_the_payer")
    current = text_hash(consent_text(mandate))
    if shown_hash != current:
        raise MandateRefused("This mandate changed after you opened it. Open it again and read what it now says.", "stale_consent")
    with transaction.atomic():
        consent = MandateConsent.objects.create(
            school=mandate.school, mandate=mandate, payer=mandate.payer, consent_version=CONSENT_VERSION, consent_text_hash=current,
            channel=ConsentChannel.PAYER_APP, consented_at=timezone.now(), recorded_by=membership,
        )
        _summarise(mandate, consent)
    return consent


def record_provider_consent(mandate, *, reference: str, at=None) -> MandateConsent | None:
    """The provider's own authorisation is the evidence (the payer activated it with their bank): keep the provider's reference. Idempotent: a
    mandate has one standing consent. The caller holds the mandate's row and saves it."""
    standing = MandateConsent.objects.filter(mandate=mandate, withdrawn_at__isnull=True).first()
    if standing is not None:
        if not mandate.consent_at:
            _summarise(mandate, standing)
        return None
    consent = MandateConsent.objects.create(
        school=mandate.school, mandate=mandate, payer=mandate.payer, consent_version=CONSENT_VERSION, channel=ConsentChannel.PROVIDER_HOSTED,
        consented_at=at or timezone.now(), provider_consent_reference=str(reference or mandate.provider_mandate_reference)[:120],
    )
    _summarise(mandate, consent)
    return consent


def withdraw(mandate, *, reason: str) -> None:
    """The consent is withdrawn (the payer cancelled). It stays on record."""
    MandateConsent.objects.filter(mandate=mandate, withdrawn_at__isnull=True).update(withdrawn_at=timezone.now(), withdrawn_reason=str(reason or "")[:300])
