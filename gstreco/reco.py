"""Matching engine: books vs portal documents (GSTR-2B for purchases, GSTR-1 for sales)."""
import copy
import difflib
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from typing import List, Optional

from .models import Doc
from .normalize import digit_core, fmt_date, norm_inv, r2

MATCHED = "Matched"
PROBABLE = "Probable match"
MISMATCH = "Amount mismatch"
GSTIN_MISMATCH = "GSTIN mismatch"
BOOKS_ONLY = "Only in books"
PORTAL_ONLY = "Only in portal"
STATUS_ORDER = [MATCHED, PROBABLE, MISMATCH, GSTIN_MISMATCH, BOOKS_ONLY, PORTAL_ONLY]


@dataclass
class RecoRow:
    status: str
    books: Optional[Doc]
    portal: Optional[Doc]
    remarks: str = ""

    def val(self, side, attr):
        d = self.books if side == "books" else self.portal
        return getattr(d, attr) if d else 0.0

    def diff(self, attr):
        return r2(self.val("books", attr) - self.val("portal", attr))

    @property
    def gstin(self):
        return (self.books or self.portal).gstin

    @property
    def party(self):
        return (self.books.party if self.books and self.books.party else
                (self.portal.party if self.portal else ""))


def _cls(d: Doc):
    return "NOTE" if d.is_note else "INV"


def _key(d: Doc):
    return (d.gstin, _cls(d), norm_inv(d.inv_no))


def _combine(docs: List[Doc]) -> Doc:
    if len(docs) == 1:
        return docs[0]
    c = copy.deepcopy(docs[0])
    for d in docs[1:]:
        for a in ("taxable", "igst", "cgst", "sgst", "cess", "total"):
            setattr(c, a, r2(getattr(c, a) + getattr(d, a)))
        c.rate_lines += d.rate_lines
        c.items += d.items
        if d.voucher_no and d.voucher_no not in c.voucher_no:
            c.voucher_no += f", {d.voucher_no}"
    c.note = (c.note + f" {len(docs)} entries combined").strip()
    return c


def _amounts_close(a: Doc, b: Doc, tol):
    return abs(a.taxable - b.taxable) <= tol and abs(a.tax - b.tax) <= tol


def _remarks(b: Doc, p: Doc, tol):
    r = []
    if b.inv_date and p.inv_date and b.inv_date != p.inv_date:
        r.append(f"Date differs (books {fmt_date(b.inv_date)}, portal {fmt_date(p.inv_date)})")
    if abs(b.taxable - p.taxable) > tol:
        r.append(f"Taxable diff {r2(b.taxable - p.taxable):,.2f}")
    if abs(b.tax - p.tax) > tol:
        r.append(f"Tax diff {r2(b.tax - p.tax):,.2f}")
    if abs(b.tax - p.tax) <= tol and (abs(b.igst - p.igst) > tol):
        r.append("Tax head differs (IGST vs CGST/SGST)")
    if p.itc_avail == "N":
        r.append(f"ITC not available per 2B{(' – ' + p.itc_reason) if p.itc_reason else ''}")
    if p.rcm:
        r.append("Reverse charge")
    for n in (b.note, p.note):
        if n:
            r.append(n)
    return "; ".join(r)


def _similarity(a: str, b: str) -> float:
    na, nb = norm_inv(a), norm_inv(b)
    s = difflib.SequenceMatcher(None, na, nb).ratio()
    dc_a, dc_b = digit_core(a), digit_core(b)
    if dc_a and dc_a == dc_b:
        s = max(s, 0.9)
    return s


def reconcile(books: List[Doc], portal: List[Doc], tol: float = 1.0) -> List[RecoRow]:
    bg, pg = defaultdict(list), defaultdict(list)
    for d in books:
        bg[_key(d)].append(d)
    for d in portal:
        pg[_key(d)].append(d)
    bdocs = {k: _combine(v) for k, v in bg.items()}
    pdocs = {k: _combine(v) for k, v in pg.items()}

    rows: List[RecoRow] = []
    # 1. exact key
    for k in list(bdocs):
        if k in pdocs:
            b, p = bdocs.pop(k), pdocs.pop(k)
            st = MATCHED if _amounts_close(b, p, tol) else MISMATCH
            rows.append(RecoRow(st, b, p, _remarks(b, p, tol)))

    # 2. same GSTIN, amounts match, invoice number similar (typos, prefixes, FY tags)
    by_gstin = defaultdict(list)
    for k, p in pdocs.items():
        by_gstin[p.gstin].append(k)
    cands = []
    for bk, b in bdocs.items():
        for pk in by_gstin.get(b.gstin, []):
            p = pdocs[pk]
            if not _amounts_close(b, p, tol):
                continue
            sim = _similarity(b.inv_no, p.inv_no)
            same_date = b.inv_date and b.inv_date == p.inv_date
            if sim >= 0.6 or (same_date and sim >= 0.3) or (_cls(b) != _cls(p) and sim >= 0.8):
                cands.append((sim + (0.2 if same_date else 0), bk, pk))
    for _, bk, pk in sorted(cands, reverse=True):
        if bk in bdocs and pk in pdocs:
            b, p = bdocs.pop(bk), pdocs.pop(pk)
            rm = f"Invoice no. differs (books '{b.inv_no}', portal '{p.inv_no}')"
            rest = _remarks(b, p, tol)
            rows.append(RecoRow(PROBABLE, b, p, rm + ("; " + rest if rest else "")))

    # 3. same invoice no. + amounts, different GSTIN (wrong GSTIN in books / supplier filed wrong)
    by_inv = defaultdict(list)
    for k, p in pdocs.items():
        by_inv[norm_inv(p.inv_no)].append(k)
    for bk in list(bdocs):
        b = bdocs[bk]
        for pk in by_inv.get(norm_inv(b.inv_no), []):
            if pk in pdocs and _amounts_close(b, pdocs[pk], tol) and pdocs[pk].gstin != b.gstin:
                p = pdocs.pop(pk)
                bdocs.pop(bk)
                rows.append(RecoRow(GSTIN_MISMATCH, b, p,
                                    f"GSTIN books {b.gstin or '(blank)'} vs portal {p.gstin}"))
                break

    # 4. leftovers
    for b in bdocs.values():
        rows.append(RecoRow(BOOKS_ONLY, b, None,
                            "Not in portal – follow up with supplier / check period"
                            if b.direction == "in" else "Not reported in GSTR-1"))
    for p in pdocs.values():
        rows.append(RecoRow(PORTAL_ONLY, None, p,
                            _remarks(p, p, tol) or ("Not booked in Tally" if p.direction == "in"
                                                    else "Reported in GSTR-1 but not in books")))
    rows.sort(key=lambda r: (STATUS_ORDER.index(r.status), r.party.lower(),
                             (r.books or r.portal).inv_date or date.min))
    return rows


def summarize(rows: List[RecoRow]):
    """[(status, count, books_taxable, books_tax, portal_taxable, portal_tax)]"""
    agg = defaultdict(lambda: [0, 0.0, 0.0, 0.0, 0.0])
    for r in rows:
        a = agg[r.status]
        a[0] += 1
        for i, (side, attr) in enumerate([("books", "taxable"), ("books", "tax"),
                                          ("portal", "taxable"), ("portal", "tax")], start=1):
            d = r.books if side == "books" else r.portal
            if d:
                a[i] += d.sign * getattr(d, attr)
    out = [(s, agg[s][0], *map(r2, agg[s][1:])) for s in STATUS_ORDER if s in agg]
    tot = [sum(x[i] for x in out) for i in range(1, 6)]
    out.append(("Total", int(tot[0]), *map(r2, tot[1:])))
    return out


def reconcile_b2cs(books_b2cs: dict, portal_b2cs: dict, tol=1.0):
    rows = []
    for k in sorted(set(books_b2cs) | set(portal_b2cs)):
        b, p = books_b2cs.get(k, {}), portal_b2cs.get(k, {})
        bt, pt = b.get("txval", 0), p.get("txval", 0)
        btax = sum(b.get(f, 0) for f in ("iamt", "camt", "samt", "csamt"))
        ptax = sum(p.get(f, 0) for f in ("iamt", "camt", "samt", "csamt"))
        st = MATCHED if abs(bt - pt) <= tol and abs(btax - ptax) <= tol else \
            (BOOKS_ONLY if not p else PORTAL_ONLY if not b else MISMATCH)
        rows.append({"pos": k[0], "rate": k[1], "status": st, "books_taxable": r2(bt),
                     "portal_taxable": r2(pt), "books_tax": r2(btax), "portal_tax": r2(ptax)})
    return rows


def status_counts(rows):
    return Counter(r.status for r in rows)
