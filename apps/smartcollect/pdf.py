"""A minimal PDF table writer using only the standard library.

A4 landscape, Helvetica, a title block, a table that repeats its header on every page, and "Page x of y". Text is written in the PDF
"WinAnsi" encoding; anything it cannot show is replaced, never allowed to break the file. Long cells are cut to fit their column.
"""

from datetime import datetime

PAGE_W, PAGE_H = 842, 595
MARGIN = 36
FONT, BOLD = "F1", "F2"
#: Helvetica's average glyph is a little over half the font size wide: enough to cut a cell to its column without measuring each glyph.
AVG = 0.52


def _clean(text) -> str:
    return str(text).replace("\r", " ").replace("\n", " ")


def _encode(text: str) -> bytes:
    raw = _clean(text).encode("cp1252", errors="replace")
    return raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def _fit(text, width: float, size: float) -> str:
    text = _clean(text)
    room = max(int(width / (size * AVG)), 1)
    return text if len(text) <= room else text[: max(room - 1, 1)] + "…"


class _Page:
    def __init__(self):
        self.ops: list[bytes] = []

    def text(self, x: float, y: float, value, *, size: float = 8, bold: bool = False, right_edge: float | None = None) -> None:
        if right_edge is not None:  # right-aligned to an edge
            x = right_edge - len(_clean(value)) * size * AVG
        self.ops.append(b"BT /%s %s Tf %.1f %.1f Td (" % ((BOLD if bold else FONT).encode(), (b"%g" % size), x, y) + _encode(value) + b") Tj ET")

    def line(self, x1: float, y: float, x2: float, width: float = 0.5) -> None:
        self.ops.append(b"%.2f w %.1f %.1f m %.1f %.1f l S" % (width, x1, y, x2, y))

    def stream(self) -> bytes:
        return b"\n".join(self.ops)


def build(title: str, meta: list[str], columns: list[tuple], rows: list[list], totals: list | None = None, footer: str = "") -> bytes:
    """`columns` is `[(heading, width in points, "l" or "r")]`. `rows` are lists of cell text. Returns the bytes of the PDF."""
    size, lead = 7.5, 12
    pages: list[_Page] = []
    page = None
    y = 0.0

    def start(first: bool) -> None:
        nonlocal page, y
        page = _Page()
        pages.append(page)
        y = PAGE_H - MARGIN
        page.text(MARGIN, y, title, size=14, bold=True)
        y -= 16
        if first:
            for line in meta:
                page.text(MARGIN, y, line, size=8)
                y -= 11
            y -= 4
        header()

    def header() -> None:
        nonlocal y
        x = MARGIN
        for heading, width, align in columns:
            if align == "r":
                page.text(0, y, _fit(heading, width, size), size=size, bold=True, right_edge=x + width - 4)
            else:
                page.text(x, y, _fit(heading, width, size), size=size, bold=True)
            x += width
        y -= 3
        page.line(MARGIN, y, MARGIN + sum(w for _, w, _ in columns), 0.8)
        y -= lead - 2

    def put(row: list, bold: bool = False) -> None:
        nonlocal y
        if y < MARGIN + 24:
            start(False)
        x = MARGIN
        for cell, (_, width, align) in zip(row, columns):
            shown = _fit(cell, width - 6, size)
            if align == "r":
                page.text(0, y, shown, size=size, bold=bold, right_edge=x + width - 4)
            else:
                page.text(x, y, shown, size=size, bold=bold)
            x += width
        y -= lead

    start(True)
    for row in rows:
        put(row)
    if totals:
        page.line(MARGIN, y + lead - 4, MARGIN + sum(w for _, w, _ in columns), 0.8)
        put(totals, bold=True)
    stamp = footer or f"Generated {datetime.now():%d %b %Y %H:%M}"
    for number, each in enumerate(pages, start=1):
        each.text(MARGIN, 20, stamp, size=7)
        each.text(0, 20, f"Page {number} of {len(pages)}", size=7, right_edge=PAGE_W - MARGIN)

    objects: list[bytes] = []
    page_ids = [5 + 2 * i for i in range(len(pages))]
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % i for i in page_ids) + b"] /Count %d >>" % len(pages))
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
    for i, each in enumerate(pages):
        content = each.stream()
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] /Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents %d 0 R >>"
            % (PAGE_W, PAGE_H, page_ids[i] + 1)
        )
        objects.append(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)
