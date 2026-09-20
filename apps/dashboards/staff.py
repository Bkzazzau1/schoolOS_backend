from collections import Counter

from apps.staff.constants import DIRECTORY, INVITE_PENDING, NONE, PROFILE, REVIEWED, SALARY, SUBMITTED

from .data import payloads


def summary(school) -> dict:
    people = payloads(school, DIRECTORY)
    profiles = {p["_id"]: p for p in payloads(school, PROFILE)}
    salaries = [s for s in payloads(school, SALARY) if s.get("onPayroll")]

    registration = Counter(p.get("onboardingStatus", NONE) for p in profiles.values())
    gross = sum(s["gross"] for s in salaries)
    deductions = sum(s["deductions"] for s in salaries)
    return {
        "headcount": len(people),
        "bySection": dict(Counter(p.get("section") or "Unassigned" for p in people)),
        "byCategory": dict(Counter(p.get("staffCategory") or "unknown" for p in people)),
        "filesComplete": sum(1 for p in people if p.get("fileStatus") == "Complete"),
        "filesMissingDocuments": sum(1 for p in people if p.get("fileStatus") == "Missing document"),
        "withLogin": sum(1 for p in profiles.values() if p.get("linkedMembershipId")),
        "registration": {
            "notRequested": registration[NONE],
            "waitingForStaff": registration[INVITE_PENDING],
            "waitingForReview": registration[SUBMITTED],
            "reviewed": registration[REVIEWED],
        },
        "onPayroll": len(salaries),
        "monthlyPayroll": {"gross": gross, "deductions": deductions, "net": gross - deductions},
    }
