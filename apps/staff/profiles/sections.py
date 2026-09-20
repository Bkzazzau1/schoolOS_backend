"""Checking each section of a staff profile.

A profile has sections (personal, academics, credentials, documents, payment,
reviews). Each function here validates one section and returns it in its stored,
normalized form (a phone number always as 08031234567, and so on). The rules about
*who may change which section* are in rules.py.
"""

from datetime import date

from apps.core.errors import Rejected
from apps.core.identity import normalize_nin, normalize_phone
from apps.core.validation import boolean, choice, integer, is_email, text

from ..constants import DOCUMENT_STATUSES, STUDY_LEVELS

PERSONAL_KEYS = (
    "phone", "nin", "email", "address", "dateOfBirth", "gender", "stateOfOrigin",
    "nextOfKinName", "nextOfKinPhone", "employmentDate", "employmentType",
)
PAYMENT_KEYS = ("bankName", "accountName", "accountNumber")


def _object(raw, label: str) -> dict:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise Rejected(f"{label} must be an object.")
    return raw


def _list(raw, label: str, limit: int) -> list:
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > limit:
        raise Rejected(f"{label} must be a list of at most {limit}.")
    if not all(isinstance(item, dict) for item in raw):
        raise Rejected(f"Every entry in {label} must be an object.")
    return raw


def _date(raw: dict, key: str, label: str) -> str:
    value = text(raw, key, max_len=10, required=False)
    if value:
        try:
            date.fromisoformat(value)
        except ValueError:
            raise Rejected(f"{label} must be a date like 1990-05-14.")
    return value


def blank_personal() -> dict:
    return {key: "" for key in PERSONAL_KEYS}


def personal(raw) -> dict:
    raw = _object(raw, "personal")
    cleaned = blank_personal()
    for key, label in (("phone", "The phone number"), ("nextOfKinPhone", "The next of kin's phone number")):
        value = text(raw, key, max_len=30, required=False)
        if value:
            normalized = normalize_phone(value)
            if normalized is None:
                raise Rejected(f"{label} is not a valid Nigerian phone number, for example 0803 123 4567.")
            cleaned[key] = normalized
    nin = text(raw, "nin", max_len=30, required=False)
    if nin:
        cleaned["nin"] = normalize_nin(nin) or _bad("A NIN is exactly 11 digits.")
    email = text(raw, "email", max_len=254, required=False).lower()
    if email and not is_email(email):
        raise Rejected("Enter a valid email address.")
    cleaned["email"] = email
    cleaned["dateOfBirth"] = _date(raw, "dateOfBirth", "The date of birth")
    cleaned["employmentDate"] = _date(raw, "employmentDate", "The employment date")
    for key, limit in (("address", 300), ("gender", 20), ("stateOfOrigin", 50),
                       ("nextOfKinName", 120), ("employmentType", 50)):
        cleaned[key] = text(raw, key, max_len=limit, required=False)
    return cleaned


def _bad(message: str):
    raise Rejected(message)


def academics(raw) -> list[dict]:
    this_year = date.today().year
    return [
        {
            "level": choice(item.get("level"), STUDY_LEVELS, "level"),
            "institution": text(item, "institution"),
            "course": text(item, "course"),
            "year": integer(item, "year", minimum=1950, maximum=this_year),
            "grade": text(item, "grade", max_len=50, required=False),
        }
        for item in _list(raw, "academics", 30)
    ]


def credentials(raw) -> list[dict]:
    return [
        {
            "title": text(item, "title"),
            "issuer": text(item, "issuer"),
            "number": text(item, "number", max_len=60, required=False),
            "expiry": _date(item, "expiry", "The expiry date"),
            "verified": boolean(item, "verified") if "verified" in item else False,
            "documentRef": text(item, "documentRef", max_len=120, required=False),
        }
        for item in _list(raw, "credentials", 30)
    ]


def documents(raw) -> list[dict]:
    cleaned = [
        {
            "name": text(item, "name", max_len=100),
            "status": choice(item.get("status", "requested"), DOCUMENT_STATUSES, "status"),
            "reference": text(item, "reference", max_len=200, required=False),
        }
        for item in _list(raw, "documents", 50)
    ]
    if len({d["name"] for d in cleaned}) != len(cleaned):
        raise Rejected("A document is listed twice.")
    return cleaned


def payment(raw) -> dict:
    raw = _object(raw, "payment")
    cleaned = {key: text(raw, key, max_len=100, required=False) for key in PAYMENT_KEYS}
    if any(cleaned.values()):
        number = cleaned["accountNumber"].replace(" ", "")
        if not (cleaned["bankName"] and cleaned["accountName"]) or not (number.isdigit() and len(number) == 10):
            raise Rejected("Enter the bank, the account name and a 10-digit account number.")
        cleaned["accountNumber"] = number
    return cleaned


def reviews_shape(raw) -> list[dict]:
    """Existing and new reviews, checked only for shape. Who wrote them and when is
    stamped by the server (see rules.check_reviews)."""
    out = []
    for item in _list(raw, "reviews", 500):
        out.append(
            {
                "period": text(item, "period", max_len=100),
                "rating": integer(item, "rating", minimum=1, maximum=5),
                "notes": text(item, "notes", max_len=1000, required=False),
                "at": item.get("at", ""),
                "reviewerRole": item.get("reviewerRole", ""),
                "reviewerMembershipId": item.get("reviewerMembershipId", ""),
            }
        )
    return out


def loaded(profile: dict | None) -> dict:
    """A stored profile with every section present, without re-validating it."""
    profile = profile or {}
    return {
        "personal": {**blank_personal(), **(profile.get("personal") or {})},
        "academics": list(profile.get("academics") or []),
        "credentials": list(profile.get("credentials") or []),
        "documents": list(profile.get("documents") or []),
        "reviews": list(profile.get("reviews") or []),
        "payment": {**{k: "" for k in PAYMENT_KEYS}, **(profile.get("payment") or {})},
        "onboardingStatus": profile.get("onboardingStatus") or "none",
        "onboardingEmail": profile.get("onboardingEmail") or "",
        "linkedMembershipId": profile.get("linkedMembershipId") or "",
        "systemRole": profile.get("systemRole") or "",
    }
