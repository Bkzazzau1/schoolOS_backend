"""How a family's payment account looks, which depends on the bank or provider that issued it.

Nigerian banks and collection providers do not all give the same kind of identifier: one gives a ten-digit
account number, another a wallet or payment code, another a reference to quote alongside a shared account.
So nothing in SchoolOS assumes an account is "ten digits". Each provider has a SHAPE that says what it calls
the identifier, what one may look like, and what else a payer must be told (a payment reference, a sort code,
how to pay). A provider with no shape of its own gets the broad default below: SchoolOS only narrows it for a
provider that has documented its format, and never guesses one.

A family may hold accounts with several providers at once (each with its own shape), and the account belongs
to the FAMILY - never to a child - so it stays the same whichever of the family's children owe.
"""

import re
from dataclasses import dataclass

from .errors import Refused

MAX_DETAILS = 6
MAX_LABEL = 40
MAX_VALUE = 120
#: Broad on purpose: letters, digits and a few separators, 4-40 characters, once spaces are removed.
GENERIC_PATTERN = r"[A-Za-z0-9][A-Za-z0-9\-_/]{2,38}[A-Za-z0-9]"


@dataclass(frozen=True)
class AccountShape:
    #: What this provider calls the identifier a payer types: "Account number", "Payment code", "Wallet ID".
    number_label: str = "Account number"
    number_pattern: str = GENERIC_PATTERN
    #: A made-up example of the format, for a form's hint. Never a real account.
    number_example: str = ""
    #: Extra facts this provider gives a payer with the number, as label names the form should offer
    #: (for example "Payment reference"). They are optional unless a provider says otherwise.
    detail_labels: tuple = ()
    #: One line for the payer: how to pay this account.
    payer_note: str = ""

    def clean_number(self, raw) -> str:
        """The identifier a payer will type, with spaces removed, or '' if none was given. Refuses one that
        cannot be this provider's."""
        number = "".join(str(raw or "").split())
        if number and not re.fullmatch(self.number_pattern, number):
            hint = f" It should look like {self.number_example}." if self.number_example else ""
            raise Refused(f"That is not a valid {self.number_label.lower()}.{hint}", "invalid_account_number")
        return number

    def as_dict(self) -> dict:
        return {
            "numberLabel": self.number_label, "numberExample": self.number_example, "detailLabels": list(self.detail_labels),
            "payerNote": self.payer_note,
        }


GENERIC = AccountShape()


def _connector(provider: str):
    from apps.bankconnect.providers import registry  # imported here: the providers package reaches back into receivables

    return registry.get_connector(str(provider or ""))


def label_for(provider: str) -> str:
    """What a provider calls the number a payer uses ("Account number"; Remita's is a payment reference). A provider SchoolOS does not
    know gets the plain word."""
    connector = _connector(provider)
    return connector.info.account_label if connector else GENERIC.number_label


def note_for(provider: str) -> str:
    """One line telling a payer how to pay this provider's account."""
    connector = _connector(provider)
    return connector.info.payer_note if connector else ""


def clean_details(details) -> list[dict]:
    """The extra facts shown to a payer with an account, as `[{"label", "value"}]`. Anything else is refused."""
    if details in (None, "", []):
        return []
    if not isinstance(details, (list, tuple)) or len(details) > MAX_DETAILS:
        raise Refused(f"An account can carry at most {MAX_DETAILS} extra facts.", "invalid_account_details")
    cleaned, seen = [], set()
    for item in details:
        if not isinstance(item, dict):
            raise Refused("Each extra fact needs a label and a value.", "invalid_account_details")
        label, value = " ".join(str(item.get("label") or "").split()), " ".join(str(item.get("value") or "").split())
        if not label or not value:
            raise Refused("Each extra fact needs a label and a value.", "invalid_account_details")
        if len(label) > MAX_LABEL or len(value) > MAX_VALUE:
            raise Refused(f"A label can be at most {MAX_LABEL} characters and a value at most {MAX_VALUE}.", "invalid_account_details")
        if label.casefold() in seen:
            raise Refused(f"'{label}' is given twice.", "invalid_account_details")
        seen.add(label.casefold())
        cleaned.append({"label": label, "value": value})
    return cleaned
