"""The lines of a batch: who is paid what.

The app sends the lines it built, but the server never trusts them. Each line's
name and amount come from the salary record the owner set, and a line that
disagrees with it is refused, so a batch always says exactly what the salary
records say.
"""

from apps.core.errors import Rejected
from apps.sync.models import SyncRecord

from .constants import MAX_LINES, SALARY


def _salaries(school) -> dict[str, dict]:
    rows = SyncRecord.objects.filter(school=school, entity_type=SALARY, deleted=False)
    return {r.entity_id: r.payload for r in rows}


def net_pay(salary: dict) -> int:
    return salary["gross"] - salary["deductions"]


def build(school, sent) -> tuple[list[dict], int]:
    """Check the lines the app sent and return the lines to store, with their total."""
    if not isinstance(sent, list) or not sent:
        raise Rejected("There are no staff in this batch.")
    if len(sent) > MAX_LINES:
        raise Rejected("This batch is too large.")
    salaries, lines, seen = _salaries(school), [], set()
    for line in sent:
        if not isinstance(line, dict) or not isinstance(line.get("staffId"), str):
            raise Rejected("Each line needs a staffId.")
        staff_id = line["staffId"]
        if staff_id in seen:
            raise Rejected("The same person is listed twice.")
        seen.add(staff_id)
        salary = salaries.get(staff_id)
        if salary is None or not salary.get("onPayroll"):
            raise Rejected(f"{line.get('name') or staff_id} is not on payroll.")
        net = net_pay(salary)
        if net <= 0:
            raise Rejected(f"{salary['name']} has no pay to disburse.")
        if line.get("net") != net or isinstance(line.get("net"), bool):
            raise Rejected(f"The amount for {salary['name']} does not match their salary. Refresh and prepare again.")
        lines.append({"staffId": staff_id, "name": salary["name"], "net": net})
    return lines, sum(line["net"] for line in lines)


def still_current(school, lines: list[dict]) -> str | None:
    """None if every line still matches the salary records, else what changed."""
    salaries = _salaries(school)
    for line in lines:
        salary = salaries.get(line["staffId"])
        if salary is None or not salary.get("onPayroll") or net_pay(salary) != line["net"]:
            return line["name"]
    return None
