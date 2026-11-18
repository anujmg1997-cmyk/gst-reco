"""Excel output (openpyxl)."""
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .normalize import fmt_date
from .reco import BOOKS_ONLY, GSTIN_MISMATCH, MATCHED, MISMATCH, PORTAL_ONLY, PROBABLE, summarize

FILLS = {
    MATCHED: "E2F0D9", PROBABLE: "FFF2CC", MISMATCH: "FCE4D6", GSTIN_MISMATCH: "FCE4D6",
    BOOKS_ONLY: "F8CBAD", PORTAL_ONLY: "DDEBF7",
}
HDR_FILL = PatternFill("solid", fgColor="1F3864")
HDR_FONT = Font(bold=True, color="FFFFFF")
NUM = "#,##0.00"


def _header(ws, row, headers):
    for c, h in enumerate(headers, start=1):
        cell = ws.cell(row=row, column=c, value=h)
        cell.fill, cell.font = HDR_FILL, HDR_FONT
        cell.alignment = Alignment(wrap_text=True, vertical="center")


def _widths(ws, widths):
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w


RECO_COLS = ["Status", "Remarks", "GSTIN", "Party", "Type", "Books inv no", "Books date", "Portal inv no",
             "Portal date", "Books taxable", "Portal taxable", "Diff taxable", "Books IGST", "Portal IGST",
             "Books CGST", "Portal CGST", "Books SGST", "Portal SGST", "Books cess", "Portal cess",
             "Books total tax", "Portal total tax", "Diff tax", "Books voucher", "2B ITC avail"]


def reco_row_values(r):
    b, p = r.books, r.portal
    g = lambda d, a: getattr(d, a) if d else None
    return [r.status, r.remarks, r.gstin, r.party, (b or p).doc_type,
            g(b, "inv_no"), fmt_date(g(b, "inv_date")), g(p, "inv_no"), fmt_date(g(p, "inv_date")),
            g(b, "taxable"), g(p, "taxable"), r.diff("taxable"),
            g(b, "igst"), g(p, "igst"), g(b, "cgst"), g(p, "cgst"), g(b, "sgst"), g(p, "sgst"),
            g(b, "cess"), g(p, "cess"), g(b, "tax"), g(p, "tax"), r.diff("tax"),
            g(b, "voucher_no"), g(p, "itc_avail")]


def write_reco(path, rows, title, b2cs_rows=None, portal_label="Portal"):
    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    ws["A1"] = title
    ws["A1"].font = Font(bold=True, size=14)
    hdr = ["Status", "Count", "Books taxable", "Books tax", f"{portal_label} taxable", f"{portal_label} tax"]
    _header(ws, 3, hdr)
    for i, srow in enumerate(summarize(rows), start=4):
        for c, v in enumerate(srow, start=1):
            cell = ws.cell(row=i, column=c, value=v)
            if c > 2:
                cell.number_format = NUM
            if srow[0] in FILLS:
                cell.fill = PatternFill("solid", fgColor=FILLS[srow[0]])
            if srow[0] == "Total":
                cell.font = Font(bold=True)
    ws.cell(row=i + 2, column=1, value="Credit notes are netted (negative) in the summary totals.")
    _widths(ws, [22, 10, 16, 16, 16, 16])

    ds = wb.create_sheet("Details")
    _header(ds, 1, RECO_COLS)
    for i, r in enumerate(rows, start=2):
        vals = reco_row_values(r)
        fill = PatternFill("solid", fgColor=FILLS.get(r.status, "FFFFFF"))
        for c, v in enumerate(vals, start=1):
            cell = ds.cell(row=i, column=c, value=v)
            if 10 <= c <= 23:
                cell.number_format = NUM
            if c == 1:
                cell.fill = fill
    ds.freeze_panes = "C2"
    ds.auto_filter.ref = f"A1:{get_column_letter(len(RECO_COLS))}{max(2, len(rows) + 1)}"
    _widths(ds, [18, 48, 18, 30, 6, 16, 11, 16, 11] + [13] * 14 + [14, 8])

    if b2cs_rows:
        bs = wb.create_sheet("B2CS")
        cols = ["POS", "Rate", "Status", "Books taxable", "Portal taxable", "Books tax", "Portal tax"]
        _header(bs, 1, cols)
        for i, r in enumerate(b2cs_rows, start=2):
            for c, k in enumerate(["pos", "rate", "status", "books_taxable", "portal_taxable",
                                   "books_tax", "portal_tax"], start=1):
                cell = bs.cell(row=i, column=c, value=r[k])
                if c > 3:
                    cell.number_format = NUM
        _widths(bs, [8, 8, 16, 16, 16, 16, 16])
    wb.save(path)
    return path


def write_3b(path, rows, title):
    wb = Workbook()
    ws = wb.active
    ws.title = "GSTR-3B working"
    ws["A1"] = title
    ws["A1"].font = Font(bold=True, size=14)
    _header(ws, 3, ["Table", "Description", "Taxable value", "IGST", "CGST", "SGST/UTGST", "Cess"])
    for i, (sec, label, v) in enumerate(rows, start=4):
        vals = [sec, label, v["taxable"], v["igst"], v["cgst"], v["sgst"], v["cess"]]
        for c, x in enumerate(vals, start=1):
            cell = ws.cell(row=i, column=c, value=x)
            if c > 2:
                cell.number_format = NUM
            if sec in ("Net", "Safe ITC"):
                cell.font = Font(bold=True)
    ws.cell(row=i + 2, column=1, value="Working figures – review before entering in GSTR-3B on the portal.")
    _widths(ws, [10, 70, 16, 14, 14, 14, 12])
    wb.save(path)
    return path


def write_books(path, docs, title):
    wb = Workbook()
    ws = wb.active
    ws.title = "Books"
    cols = ["Dir", "Type", "Voucher type", "Voucher no", "Inv no", "Date", "Party", "GSTIN", "POS",
            "Taxable", "IGST", "CGST", "SGST", "Cess", "Total", "Rates", "Warnings"]
    _header(ws, 1, cols)
    for i, d in enumerate(docs, start=2):
        vals = [d.direction, d.doc_type, d.voucher_type, d.voucher_no, d.inv_no, fmt_date(d.inv_date),
                d.party, d.gstin, d.pos, d.taxable, d.igst, d.cgst, d.sgst, d.cess, d.total,
                ", ".join(f"{rl.rate:g}%" for rl in d.rate_lines), "; ".join(d.warnings)]
        for c, v in enumerate(vals, start=1):
            cell = ws.cell(row=i, column=c, value=v)
            if 10 <= c <= 15:
                cell.number_format = NUM
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:Q{max(2, len(docs) + 1)}"
    _widths(ws, [5, 5, 14, 12, 16, 11, 30, 18, 5, 13, 12, 12, 12, 10, 13, 10, 50])
    wb.save(path)
    return path
