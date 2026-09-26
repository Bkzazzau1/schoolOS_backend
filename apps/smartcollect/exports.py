"""The batch preview as a PDF or an Excel workbook, for a maker to review and a checker to keep.

An export reads what is STORED in the batch - the same figures the checker approves - and never calls a provider, so it costs nothing and
changes nothing. It carries the batch's version and fingerprint, so a printed copy can be matched to exactly what was approved.
"""

from django.utils import timezone

from apps.receivables import periods
from apps.receivables.models import FamilyGuardian

from . import pdf, xlsx
from .constants import Eligibility, GenerationStatus
from .models import CollectionGenerationBatchItem

CONTENT_TYPES = {"pdf": "application/pdf", "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}


def naira(minor: int) -> str:
    return f"{minor / 100:,.2f}"


def _person(membership) -> str:
    if membership is None:
        return ""
    user = membership.user
    return (user.get_full_name() or user.email) if user else ""


def _payers(batch) -> dict:
    found: dict = {}
    for link in FamilyGuardian.objects.select_related("guardian").filter(school=batch.school, is_active=True):
        current = found.get(link.family_id)
        if current is None or (link.is_primary_payer and not current[1]):
            found[link.family_id] = (link.guardian.name, link.is_primary_payer)
    return {family_id: name for family_id, (name, _) in found.items()}


def _table(batch, *, only_selected: bool):
    items = list(CollectionGenerationBatchItem.objects.select_related("family").filter(batch=batch))
    if only_selected:
        items = [i for i in items if i.selected]
    payers = _payers(batch)
    rows = []
    for item in items:
        policy = (item.policy_snapshot or {}).get("values") or {}
        rows.append({
            "code": item.family.code, "family": item.family.display_name, "payer": payers.get(item.family_id, ""),
            "eligibility": Eligibility(item.eligibility_status).label, "selected": item.selected,
            "previous": item.previous_arrears_minor, "current": item.current_due_minor, "target": item.proposed_collection_minor,
            "arrears": item.get_arrears_policy_display(), "type": str(policy.get("account_mode", "")).title(),
            "override": item.override_reason if item.eligibility_override else "", "note": item.eligibility_note,
            "status": GenerationStatus(item.generation_status).label if item.selected else "",
        })
    return rows


def _meta(batch) -> list[str]:
    period = periods.label(batch.session, batch.term)
    return [
        f"{batch.school.name} - collection batch {batch.title or str(batch.id)[:8]} - {period}",
        f"Provider: {batch.provider.title()} ({batch.environment}) - Status: {batch.get_status_display()} - Version {batch.version}",
        f"Prepared by {_person(batch.prepared_by)} on {timezone.localtime(batch.prepared_at):%d %b %Y %H:%M}. Fingerprint {batch.snapshot_hash[:16]}",
        f"Selected {batch.selected_count} of {batch.family_count} families - previous balances NGN {naira(batch.total_previous_arrears_minor)}"
        f" - current due NGN {naira(batch.total_current_due_minor)} - to be collected NGN {naira(batch.total_collection_minor)}",
    ]


def as_xlsx(batch, *, only_selected: bool = False) -> bytes:
    rows = _table(batch, only_selected=only_selected)
    head = ["Family code", "Family", "Payer", "Eligibility", "Selected", "Previous balance (NGN)", "Current due (NGN)", "Collection target (NGN)",
            "Previous balances", "Account type", "Override reason", "Note", "Generation"]
    sheet = [[xlsx.Cell(h, xlsx.HEADER) for h in head]]
    for r in rows:
        sheet.append([
            r["code"], r["family"], r["payer"], r["eligibility"], "Yes" if r["selected"] else "No", xlsx.Cell(r["previous"] / 100, xlsx.MONEY),
            xlsx.Cell(r["current"] / 100, xlsx.MONEY), xlsx.Cell(r["target"] / 100, xlsx.MONEY), r["arrears"], r["type"], r["override"], r["note"], r["status"],
        ])
    chosen = [r for r in rows if r["selected"]]
    sheet.append([])
    sheet.append([
        xlsx.Cell("Selected families", xlsx.HEADER), len(chosen), "", "", "", xlsx.Cell(sum(r["previous"] for r in chosen) / 100, xlsx.MONEY),
        xlsx.Cell(sum(r["current"] for r in chosen) / 100, xlsx.MONEY), xlsx.Cell(sum(r["target"] for r in chosen) / 100, xlsx.MONEY),
    ])
    summary = [[xlsx.Cell("Collection batch", xlsx.HEADER)], *[[line] for line in _meta(batch)]]
    return xlsx.build([("Families", sheet, [12, 28, 22, 22, 9, 18, 16, 20, 20, 12, 30, 40, 16]), ("Batch", summary, [120])])


def as_pdf(batch, *, only_selected: bool = False) -> bytes:
    rows = _table(batch, only_selected=only_selected)
    columns = [
        ("Code", 62, "l"), ("Family", 150, "l"), ("Payer", 110, "l"), ("Eligibility", 92, "l"), ("Sel.", 26, "l"), ("Previous", 72, "r"),
        ("Current", 72, "r"), ("To collect", 78, "r"), ("Override / note", 138, "l"),
    ]
    body = [
        [r["code"], r["family"], r["payer"], r["eligibility"], "Yes" if r["selected"] else "No", naira(r["previous"]), naira(r["current"]), naira(r["target"]),
         r["override"] or r["note"]]
        for r in rows
    ]
    chosen = [r for r in rows if r["selected"]]
    totals = ["", f"Selected: {len(chosen)}", "", "", "", naira(sum(r["previous"] for r in chosen)), naira(sum(r["current"] for r in chosen)),
              naira(sum(r["target"] for r in chosen)), ""]
    return pdf.build("Collection accounts - batch preview", _meta(batch), columns, body, totals, footer=f"Batch {str(batch.id)[:8]} v{batch.version} - amounts in NGN")


def build(batch, fmt: str, *, only_selected: bool = False) -> tuple[bytes, str, str]:
    """`(bytes, content type, file name)` for `pdf` or `xlsx`."""
    fmt = str(fmt or "").lower()
    if fmt not in CONTENT_TYPES:
        raise ValueError("Choose pdf or xlsx.")
    data = as_pdf(batch, only_selected=only_selected) if fmt == "pdf" else as_xlsx(batch, only_selected=only_selected)
    return data, CONTENT_TYPES[fmt], f"collection-batch-{str(batch.id)[:8]}-v{batch.version}.{fmt}"
