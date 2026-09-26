"""Working out which student a payment is for - and saying why.

Everything here is a pure function of the payment and the school's own student directory: no
database writes, no guessing. Each clue that fits adds points and is recorded in words, so a person
reviewing a payment can see exactly what the engine saw. Three rules keep it honest:

* A clue shared by every sibling (the guardian's name or phone) can never pick one child. Only a
  clue that names the child - the student code or admission number in the narration - can.
* Two students that fit almost equally well are never "matched": that goes to a person.
* Weak evidence is shown, not acted on. Below `POSSIBLE_AT` nothing is suggested as a match.

Bump `ENGINE_VERSION` whenever a weight or rule changes, so a stored result says which rules made it.
The expected fee amount is deliberately not a clue yet: there is no server-side fee ledger to ask.
"""

import re
import unicodedata
from dataclasses import dataclass, field

from apps.students.models import EnrollmentStatus, GuardianLink, Student, StudentEnrollment, StudentStatus

from .constants import ReconStatus

ENGINE_VERSION = "1"

#: At or above this, and clearly ahead of everyone else, a payment is matched without a person.
MATCHED_AT = 90
#: At or above this, the top student is suggested to a person. Below it, nothing is.
POSSIBLE_AT = 60
#: Two students closer than this are ambiguous, whatever their scores.
AMBIGUOUS_GAP = 15
#: Clues below this are noise and are left out of the explanation.
MIN_SHOWN = 10
MAX_CANDIDATES = 5

W_STUDENT_CODE = 90
W_ADMISSION_NUMBER = 80
W_SHORT_REFERENCE = 30  # a short all-digit code could be anything: a date, an amount
W_GUARDIAN_NAME_FULL = 35
W_GUARDIAN_NAME_PART = 10
W_GUARDIAN_PHONE = 30
W_SENDER_TAIL = 10
W_STUDENT_NAME_FULL = 30
W_STUDENT_NAME_PART = 5

_PHONE = re.compile(r"(?<!\d)(?:\+?234[\s-]?|0)?([789][01]\d)[\s-]?(\d{3})[\s-]?(\d{4})(?!\d)")


def tokens(text: str) -> list[str]:
    """Upper-case letters and digits, accents removed: `Bg-0042/Tuition` -> BG, 0042, TUITION."""
    plain = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    return re.findall(r"[A-Z0-9]+", plain.upper())


def _compact(text: str) -> str:
    return "".join(tokens(text))


def _strong(compact: str) -> bool:
    """A code specific enough to trust: letters and digits together, or a long number."""
    return len(compact) >= 6 or (len(compact) >= 4 and compact.isalnum() and not compact.isdigit() and not compact.isalpha())


def _reference_in(text_tokens: list[str], reference: str) -> bool:
    """Whether the reference appears as whole tokens, however the sender punctuated it."""
    target = _compact(reference)
    if not target:
        return False
    for start in range(len(text_tokens)):
        joined = ""
        for token in text_tokens[start : start + 6]:
            joined += token
            if joined == target:
                return True
            if len(joined) >= len(target):
                break
    return False


def phone_key(value: str) -> str | None:
    """A Nigerian mobile number reduced to its ten significant digits, however it was written."""
    digits = re.sub(r"\D", "", str(value or ""))
    if digits.startswith("234") and len(digits) >= 13:
        digits = digits[3:]
    digits = digits.lstrip("0")
    return digits[-10:] if len(digits) >= 10 else None


def _phones_in(text: str) -> set[str]:
    return {"".join(found) for found in _PHONE.findall(str(text or ""))}


def _name_tokens(name: str) -> set[str]:
    return {t for t in tokens(name) if len(t) >= 2}  # initials say too little


@dataclass
class Person:
    """One student as the engine sees them."""

    id: str
    code: str
    admission_number: str
    first: set
    surname: set
    display_name: str
    status: str
    class_name: str = ""
    guardians: list = field(default_factory=list)  # name-token sets
    phones: set = field(default_factory=set)


@dataclass
class Candidate:
    student_id: str
    student_name: str
    student_code: str
    class_name: str
    status: str
    score: int
    signals: list

    def as_dict(self) -> dict:
        return {
            "kind": "candidate", "studentId": self.student_id, "studentName": self.student_name,
            "studentCode": self.student_code, "className": self.class_name, "studentStatus": self.status,
            "score": self.score, "signals": self.signals,
        }


@dataclass
class Verdict:
    status: str
    confidence: int
    candidates: list
    notes: list
    #: Set when the payment was made into a family's own collection account: the family is then known for
    #: certain, and no guess about a student was needed.
    family: object = None

    @property
    def top(self) -> Candidate | None:
        return self.candidates[0] if self.candidates else None

    def reasons(self) -> list:
        found = []
        if self.family is not None:
            found.append({"kind": "family", "familyId": str(self.family.id), "familyCode": self.family.code, "familyName": self.family.display_name})
        return found + [c.as_dict() for c in self.candidates] + [{"kind": "note", "text": n} for n in self.notes]


class Directory:
    """A school's students and guardians, loaded once and reused for a whole batch of payments."""

    def __init__(self, school):
        guardians = {}
        for link in GuardianLink.objects.filter(student__school=school):
            guardians.setdefault(link.student_id, []).append(link)
        classes = dict(
            StudentEnrollment.objects.filter(school=school, status=EnrollmentStatus.ACTIVE)
            .order_by("started_at").values_list("student_id", "class_name")
        )
        self.people = []
        for s in Student.objects.filter(school=school):
            links = guardians.get(s.id, [])
            self.people.append(
                Person(
                    id=str(s.id), code=s.student_code, admission_number=s.admission_number,
                    first=_name_tokens(s.first_name), surname=_name_tokens(s.surname), display_name=s.full_name,
                    status=s.status, class_name=classes.get(s.id, ""),
                    guardians=[_name_tokens(g.name) for g in links],
                    phones={k for k in (phone_key(g.phone) for g in links) if k},
                )
            )

    def candidates(self, *, narration: str, reference: str, sender_name: str, sender_tail: str) -> list[Candidate]:
        text = tokens(f"{narration} {reference}")
        text_set = set(text)
        sender = _name_tokens(sender_name)
        phones = _phones_in(f"{narration} {reference}")
        found = []
        for person in self.people:
            signals = self._signals(person, text, text_set, sender, phones, sender_tail)
            score = min(100, sum(s["points"] for s in signals))
            if score >= MIN_SHOWN:
                found.append(
                    Candidate(person.id, person.display_name, person.code, person.class_name, person.status, score, signals)
                )
        found.sort(key=lambda c: (-c.score, c.student_name))
        return found[:MAX_CANDIDATES]

    @staticmethod
    def _signals(person, text, text_set, sender, phones, sender_tail) -> list:
        signals = []

        def add(signal, points, detail):
            signals.append({"signal": signal, "points": points, "detail": detail})

        for label, weight, reference in (
            ("student_code", W_STUDENT_CODE, person.code),
            ("admission_number", W_ADMISSION_NUMBER, person.admission_number),
        ):
            if _reference_in(text, reference):
                strong = _strong(_compact(reference))
                add(label if strong else f"{label}_short", weight if strong else W_SHORT_REFERENCE,
                    f"'{reference}' appears in the narration" if strong else f"'{reference}' appears in the narration, but is too short to be sure")

        best = max((len(sender & g) for g in person.guardians), default=0)
        if best >= 2:
            add("guardian_name", W_GUARDIAN_NAME_FULL, "The sender's name matches a guardian's name")
        elif best == 1:
            add("guardian_name_part", W_GUARDIAN_NAME_PART, "The sender shares one name with a guardian")
        if phones & person.phones:
            add("guardian_phone", W_GUARDIAN_PHONE, "A guardian's phone number appears in the narration")
        if sender_tail and any(p.endswith(sender_tail) for p in person.phones):
            add("sender_account_tail", W_SENDER_TAIL, "The sender's account ends like a guardian's phone number")

        if person.first and person.surname and person.first <= text_set and person.surname <= text_set:
            add("student_name", W_STUDENT_NAME_FULL, "The student's name appears in the narration")
        elif person.surname and person.surname <= text_set:
            add("student_surname", W_STUDENT_NAME_PART, "The student's surname appears in the narration")
        return signals


def decide(candidates: list[Candidate]) -> Verdict:
    """From the ranked candidates to a status. Never `matched` unless one student clearly stands out."""
    if not candidates:
        return Verdict(ReconStatus.UNMATCHED, 0, [], ["Nothing in the payment points to a student."])
    top = candidates[0]
    second = candidates[1] if len(candidates) > 1 else None
    if second and second.score >= POSSIBLE_AT and top.score - second.score < AMBIGUOUS_GAP:
        tied = [c for c in candidates if c.score >= POSSIBLE_AT and top.score - c.score < AMBIGUOUS_GAP]
        return Verdict(
            ReconStatus.REQUIRES_REVIEW, top.score, candidates,
            [f"{len(tied)} students fit about equally well (for example siblings sharing a guardian). A person must choose."],
        )
    if top.score >= MATCHED_AT:
        if top.status == StudentStatus.ACTIVE:
            return Verdict(ReconStatus.MATCHED, top.score, candidates, [])
        return Verdict(
            ReconStatus.POSSIBLE_MATCH, top.score, candidates,
            [f"The best match is a student whose status is '{top.status}', so it was not matched automatically."],
        )
    if top.score >= POSSIBLE_AT:
        return Verdict(ReconStatus.POSSIBLE_MATCH, top.score, candidates, [])
    return Verdict(
        ReconStatus.UNMATCHED, top.score, candidates,
        ["Some clues were found but not enough to suggest a student. They are shown for the reviewer."],
    )
