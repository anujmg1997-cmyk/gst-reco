"""GSTR-3B working from books + GSTR-2B (+ purchase reco)."""
from collections import defaultdict

from .normalize import STATE_CODES, clean_gstin, is_valid_gstin, r2
from .reco import MATCHED, PROBABLE

HEADS = ("taxable", "igst", "cgst", "sgst", "cess")


def _sum(docs, pred=lambda d: True):
    t = dict.fromkeys(HEADS, 0.0)
    for d in docs:
        if pred(d):
            for h in HEADS:
                t[h] += d.sign * getattr(d, h)
    return {h: r2(v) for h, v in t.items()}


def compute_3b(out_docs, in_books, portal_2b, purchase_rows=None, company_gstin=""):
    """Returns list of (section, label, dict(taxable, igst, cgst, sgst, cess)) rows."""
    home = clean_gstin(company_gstin)[:2]
    rows = []
    rows.append(("3.1(a)", "Outward taxable supplies (books)", _sum(out_docs, lambda d: d.tax > 0)))
    rows.append(("3.1(c)", "Nil-rated / exempt / no-tax outward (books, verify)",
                 _sum(out_docs, lambda d: d.tax == 0)))
    rcm = _sum(portal_2b, lambda d: d.rcm)
    rows.append(("3.1(d)", "Inward supplies liable to reverse charge (per 2B)", rcm))

    # 3.2 inter-state supplies to unregistered persons by place of supply
    by_pos = defaultdict(list)
    for d in out_docs:
        pos = d.pos or home
        if d.tax > 0 and not is_valid_gstin(d.gstin) and pos != home:
            by_pos[pos].append(d)
    for pos, docs in sorted(by_pos.items()):
        rows.append(("3.2", f"Inter-state to unregistered – {pos} {STATE_CODES.get(pos, '')}", _sum(docs)))

    rows.append(("4(A)(3)", "ITC – inward supplies liable to reverse charge (per 2B)",
                 {**rcm, "taxable": 0.0}))
    itc_2b = _sum(portal_2b, lambda d: not d.rcm and d.itc_avail != "N")
    rows.append(("4(A)(5)", "ITC – all other ITC available per GSTR-2B", itc_2b))
    rows.append(("4(B)", "ITC not available per 2B (itcavl = N) – reverse / do not claim",
                 _sum(portal_2b, lambda d: d.itc_avail == "N")))
    rows.append(("Books", "ITC as per purchase books", _sum(in_books)))
    if purchase_rows is not None:
        matched = [r.portal for r in purchase_rows
                   if r.status in (MATCHED, PROBABLE) and r.portal and r.portal.itc_avail != "N"
                   and not r.portal.rcm]
        rows.append(("Safe ITC", "ITC in 2B that also matches books (matched + probable)", _sum(matched)))

    # net liability (before ITC set-off and cash ledger)
    out = rows[0][2]
    net = {h: r2(out[h] - itc_2b[h]) for h in HEADS if h != "taxable"}
    net["taxable"] = 0.0
    rows.append(("Net", "Indicative output tax − ITC per 2B (before cross-utilisation; "
                        "negative = carry forward)", net))
    rows.append(("Cash", "RCM tax payable in cash (ITC of it claimed in 4(A)(3))", {**rcm, "taxable": 0.0}))
    return rows
