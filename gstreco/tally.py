"""TallyPrime connector (XML over HTTP, default port 9000) and voucher -> GST document conversion.

Enable in TallyPrime:  F1 (Help) > Settings > Connectivity > Client/Server configuration
    TallyPrime acts as: Both (or Server),  Enable ODBC: Yes,  Port: 9000
The company must be open (loaded) in Tally while fetching.
"""
import os
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import date, timedelta
from xml.sax.saxutils import escape

from .models import Doc, ItemLine, RateLine
from .normalize import (clean_gstin, is_valid_gstin, parse_date, r2, snap_rate, state_code,
                        to_float)


class TallyError(Exception):
    pass


_BAD_REF = re.compile(r"&#(?:[0-8]|1[0-9]|2[0-9]|3[01]);")
_BAD_CHR = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _clean_xml(raw: bytes) -> str:
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = raw.decode("utf-16")
    else:
        text = raw.decode("utf-8", errors="replace")
    text = _BAD_REF.sub("", text)
    text = _BAD_CHR.sub("", text)
    return re.sub(r"^\s*<\?xml[^>]*\?>", "", text)


def parse_xml(raw: bytes) -> ET.Element:
    try:
        return ET.fromstring(_clean_xml(raw))
    except ET.ParseError as e:
        raise TallyError(f"Could not read Tally XML: {e}")


def _txt(el, tag) -> str:
    v = el.findtext(tag)
    return v.strip() if v else ""


def _name(el) -> str:
    return (el.get("NAME") or _txt(el, "NAME")).strip()


def _yes(v) -> bool:
    return str(v).strip().lower() == "yes"


def _collection_request(coll, type_, fetch, company=None, d_from=None, d_to=None,
                        filters=None, systems="") -> str:
    sv = "<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>"
    if company:
        sv += f"<SVCURRENTCOMPANY>{escape(company)}</SVCURRENTCOMPANY>"
    if d_from and d_to:
        sv += (f"<SVFROMDATE TYPE=\"Date\">{d_from:%Y%m%d}</SVFROMDATE>"
               f"<SVTODATE TYPE=\"Date\">{d_to:%Y%m%d}</SVTODATE>")
    flt = f"<FILTERS>{','.join(filters)}</FILTERS>" if filters else ""
    return (
        "<ENVELOPE><HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>"
        f"<TYPE>Collection</TYPE><ID>{coll}</ID></HEADER><BODY><DESC>"
        f"<STATICVARIABLES>{sv}</STATICVARIABLES><TDL><TDLMESSAGE>"
        f"<COLLECTION NAME=\"{coll}\" ISMODIFY=\"No\"><TYPE>{type_}</TYPE><FETCH>{fetch}</FETCH>{flt}"
        f"</COLLECTION>{systems}</TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>"
    )


VOUCHER_FETCH = ("DATE,VOUCHERNUMBER,VOUCHERTYPENAME,PARTYLEDGERNAME,PARTYNAME,PARTYGSTIN,"
                 "REFERENCE,REFERENCEDATE,PLACEOFSUPPLY,STATENAME,ISCANCELLED,ISOPTIONAL,ISDELETED,"
                 "ALLLEDGERENTRIES.LIST,LEDGERENTRIES.LIST,ALLINVENTORYENTRIES.LIST,INVENTORYENTRIES.LIST")
VOUCHER_FILTER = ('<SYSTEM TYPE="Formulae" NAME="GSTRVchFilter">NOT $IsCancelled AND NOT $IsOptional'
                  ' AND ($$IsSales:$VoucherTypeName OR $$IsPurchase:$VoucherTypeName OR'
                  ' $$IsCreditNote:$VoucherTypeName OR $$IsDebitNote:$VoucherTypeName)</SYSTEM>')


class TallyClient:
    def __init__(self, host="localhost", port=9000, timeout=180):
        self.host, self.port, self.timeout = host, int(port), timeout

    @property
    def url(self):
        return f"http://{self.host}:{self.port}"

    def post_raw(self, xml: str) -> bytes:
        req = urllib.request.Request(self.url, data=xml.encode("utf-8"),
                                     headers={"Content-Type": "text/xml;charset=utf-8"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.read()
        except (urllib.error.URLError, OSError) as e:
            raise TallyError(
                f"Could not reach Tally at {self.url}. Open TallyPrime with the company loaded and "
                f"enable the HTTP/ODBC server on port {self.port} (F1 > Settings > Connectivity). [{e}]")

    def post(self, xml: str) -> ET.Element:
        root = parse_xml(self.post_raw(xml))
        err = root.findtext(".//LINEERROR")
        if err:
            raise TallyError(f"Tally returned an error: {err.strip()}")
        return root

    def list_companies(self):
        root = self.post(_collection_request("GSTRCompanies", "Company", "NAME"))
        return sorted({_name(c) for c in root.iter("COMPANY") if _name(c)})

    def fetch_masters_raw(self, company):
        parts = [
            ("GSTRLedgers", "Ledger", "NAME,PARENT,PARTYGSTIN,GSTDUTYHEAD,TAXTYPE,LEDSTATENAME,"
                                      "GSTREGISTRATIONTYPE,LEDGSTREGDETAILS.LIST"),
            ("GSTRGroups", "Group", "NAME,PARENT"),
            ("GSTRVchTypes", "VoucherType", "NAME,PARENT"),
            ("GSTRItems", "StockItem", "NAME,BASEUNITS,GSTDETAILS.LIST,HSNDETAILS.LIST"),
        ]
        return [self.post_raw(_collection_request(c, t, f, company)) for c, t, f in parts]

    def fetch_vouchers_raw(self, company, d_from, d_to) -> bytes:
        return self.post_raw(_collection_request("GSTRVouchers", "Voucher", VOUCHER_FETCH, company,
                                                 d_from, d_to, ["GSTRVchFilter"], VOUCHER_FILTER))


# ------------------------------------------------------------------ masters
class TallyMasters:
    def __init__(self):
        self.ledgers = {}   # name -> dict(parent, gstin, duty_head, state)
        self.groups = {}    # name -> parent
        self.vtypes = {}    # name -> parent
        self.items = {}     # name -> dict(unit, gst=[(from, hsn, rate)])
        self._under = {}

    def load(self, root: ET.Element):
        for l in root.iter("LEDGER"):
            n = _name(l)
            if not n:
                continue
            gstin = _txt(l, "PARTYGSTIN")
            if not gstin:
                for g in l.iter("GSTIN"):
                    if g.text and g.text.strip():
                        gstin = g.text.strip()
            self.ledgers[n] = {
                "parent": _txt(l, "PARENT"), "gstin": clean_gstin(gstin),
                "duty_head": _txt(l, "GSTDUTYHEAD"), "tax_type": _txt(l, "TAXTYPE"),
                "state": _txt(l, "LEDSTATENAME"),
            }
        for g in root.iter("GROUP"):
            if _name(g):
                self.groups[_name(g)] = _txt(g, "PARENT")
        for v in root.iter("VOUCHERTYPE"):
            if _name(v):
                self.vtypes[_name(v)] = _txt(v, "PARENT")
        for s in root.iter("STOCKITEM"):
            n = _name(s)
            if not n:
                continue
            gst = []
            for gd in s.iter("GSTDETAILS.LIST"):
                af = parse_date(_txt(gd, "APPLICABLEFROM"))
                hsn = _txt(gd, "HSNCODE")
                igst = central = None
                for rd in gd.iter("RATEDETAILS.LIST"):
                    head = _txt(rd, "GSTRATEDUTYHEAD").lower()
                    if "integrated" in head:
                        igst = to_float(_txt(rd, "GSTRATE"))
                    elif "central" in head:
                        central = to_float(_txt(rd, "GSTRATE"))
                rate = igst if igst is not None else (central * 2 if central is not None else None)
                gst.append((af, hsn, rate))
            for hd in s.iter("HSNDETAILS.LIST"):
                hsn = _txt(hd, "HSNCODE")
                if hsn:
                    gst.append((parse_date(_txt(hd, "APPLICABLEFROM")), hsn, None))
            self.items[n] = {"unit": _txt(s, "BASEUNITS"), "gst": gst}
        return self

    def group_chain(self, g):
        chain, seen = [], set()
        while g and g not in seen and g.lower() != "primary":
            chain.append(g)
            seen.add(g)
            g = self.groups.get(g, "")
        return chain

    def ledger_under(self, ledger, group) -> bool:
        key = (ledger, group)
        hit = self._under.get(key)
        if hit is None:
            info = self.ledgers.get(ledger)
            hit = bool(info) and group.lower() in (x.lower() for x in self.group_chain(info["parent"]))
            self._under[key] = hit
        return hit

    def vtype_base(self, name) -> str:
        base, seen = name, set()
        while base in self.vtypes and self.vtypes[base] and self.vtypes[base] != base \
                and base not in seen and self.vtypes[base].lower() != "primary":
            seen.add(base)
            base = self.vtypes[base]
        if base in ("Sales", "Purchase", "Credit Note", "Debit Note"):
            return base
        low = name.lower()   # fallback when masters are missing (file mode)
        if "credit" in low:
            return "Credit Note"
        if "debit" in low:
            return "Debit Note"
        if "sale" in low or "invoice" in low:
            return "Sales"
        if "purchase" in low:
            return "Purchase"
        return base

    def item_gst(self, item, on_date):
        info = self.items.get(item)
        if not info:
            return "", None, ""
        hsn, rate = "", None
        entries = sorted(info["gst"], key=lambda e: e[0] or parse_date("19000101"))
        for af, h, rt in entries:
            if af and on_date and af > on_date:
                continue
            hsn = h or hsn
            rate = rt if rt is not None else rate
        return hsn, rate, info["unit"]


# ------------------------------------------------------------------ conversion
_TAX_PATTERNS = [("igst", r"\bIGST\b|INTEGRATED"), ("cgst", r"\bCGST\b|CENTRAL\s*TAX"),
                 ("sgst", r"\bSGST\b|\bUTGST\b|STATE\s*TAX|UT\s*TAX"), ("cess", r"\bCESS\b")]
_PARTY_GROUPS = ("Sundry Debtors", "Sundry Creditors", "Cash-in-Hand", "Bank Accounts")
_BASE_MAP = {"Sales": ("out", "INV"), "Credit Note": ("out", "CN"),
             "Purchase": ("in", "INV"), "Debit Note": ("in", "DN")}
_UQC = {"NOS": "NOS", "NO": "NOS", "NUMBER": "NOS", "PCS": "PCS", "PC": "PCS", "PIECE": "PCS",
        "KG": "KGS", "KGS": "KGS", "KILOGRAM": "KGS", "GM": "GMS", "GMS": "GMS", "GRAM": "GMS",
        "LTR": "LTR", "LITRE": "LTR", "L": "LTR", "ML": "MLT", "MTR": "MTR", "METER": "MTR",
        "BOX": "BOX", "BAG": "BAG", "SET": "SET", "PAC": "PAC", "PKT": "PAC", "DOZ": "DOZ",
        "TON": "TON", "MT": "MTS", "SQM": "SQM", "SQF": "SQF", "SQFT": "SQF", "UNT": "UNT",
        "BTL": "BTL", "CTN": "CTN", "ROL": "ROL", "PRS": "PRS", "PAIR": "PRS"}


def to_uqc(unit: str) -> str:
    u = re.sub(r"[^A-Z]", "", str(unit or "").upper())
    return _UQC.get(u, "OTH" if u else "NA")


def tax_head(name, info):
    dh = (info or {}).get("duty_head", "").lower()
    if dh:
        if "integrated" in dh:
            return "igst"
        if "central" in dh:
            return "cgst"
        if "state" in dh or "ut" in dh:
            return "sgst"
        if "cess" in dh:
            return "cess"
    if info and info.get("tax_type") and info["tax_type"].upper() not in ("GST", ""):
        return None
    up = name.upper()
    if re.search(r"\bTDS\b|\bTCS\b", up):
        return None
    for head, pat in _TAX_PATTERNS:
        if re.search(pat, up):
            return head
    return None


def _ledger_lines(v):
    lines = []
    for e in v.findall("ALLLEDGERENTRIES.LIST") or v.findall("LEDGERENTRIES.LIST"):
        n = _txt(e, "LEDGERNAME")
        if n:
            lines.append((n, to_float(_txt(e, "AMOUNT"))))
    inv = v.findall("ALLINVENTORYENTRIES.LIST") or v.findall("INVENTORYENTRIES.LIST")
    for ie in inv:
        for a in ie.findall("ACCOUNTINGALLOCATIONS.LIST"):
            n = _txt(a, "LEDGERNAME")
            if n:
                lines.append((n, to_float(_txt(a, "AMOUNT"))))
    return lines, inv


def _parse_qty(s):
    m = re.match(r"\s*(-?[\d,]*\.?\d+)\s*([A-Za-z.]*)", s or "")
    if not m:
        return 0.0, ""
    return abs(to_float(m.group(1))), m.group(2)


def voucher_to_doc(v, masters: TallyMasters, company_state: str):
    vtname = _txt(v, "VOUCHERTYPENAME") or v.get("VCHTYPE", "")
    base = masters.vtype_base(vtname)
    if base not in _BASE_MAP:
        return None
    direction, dtype = _BASE_MAP[base]
    vdate = parse_date(_txt(v, "DATE"))
    party = _txt(v, "PARTYLEDGERNAME") or _txt(v, "PARTYNAME")
    lines, inv_entries = _ledger_lines(v)

    heads = defaultdict(float)
    other = party_amt = 0.0
    for name, amt in lines:
        if name == party:
            party_amt += amt
            continue
        info = masters.ledgers.get(name)
        h = tax_head(name, info)
        if h:
            heads[h] += amt
        elif re.search(r"ROUND", name, re.I):
            continue
        elif masters.ledger_under(name, "Duties & Taxes"):
            continue                       # TDS / TCS etc. – not part of GST taxable value
        elif any(masters.ledger_under(name, g) for g in _PARTY_GROUPS):
            if not party:
                party, party_amt = name, party_amt + amt
            continue
        else:
            other += amt

    d = Doc(source="books", direction=direction, doc_type=dtype, voucher_type=vtname,
            voucher_no=_txt(v, "VOUCHERNUMBER"), party=party)
    d.taxable = r2(abs(other))
    d.igst, d.cgst, d.sgst, d.cess = (r2(abs(heads[h])) for h in ("igst", "cgst", "sgst", "cess"))
    d.total = r2(abs(party_amt)) if party_amt else r2(d.taxable + d.tax)

    pinfo = masters.ledgers.get(party, {})
    d.gstin = clean_gstin(_txt(v, "PARTYGSTIN") or pinfo.get("gstin", ""))
    if d.gstin and not is_valid_gstin(d.gstin):
        d.warnings.append(f"Invalid GSTIN {d.gstin}")
    if direction == "in":
        d.inv_no = _txt(v, "REFERENCE") or d.voucher_no
        d.inv_date = parse_date(_txt(v, "REFERENCEDATE")) or vdate
        if not _txt(v, "REFERENCE"):
            d.warnings.append("Supplier invoice no. (Reference) blank – voucher no. used")
    else:
        d.inv_no, d.inv_date = d.voucher_no, vdate
    d.pos = (state_code(_txt(v, "PLACEOFSUPPLY")) or (d.gstin[:2] if d.gstin else "")
             or state_code(_txt(v, "STATENAME")) or state_code(pinfo.get("state")) or company_state)

    # ---- items & rate split
    item_rows = []
    for ie in inv_entries:
        nm = _txt(ie, "STOCKITEMNAME")
        if not nm:
            continue
        qty, unit = _parse_qty(_txt(ie, "BILLEDQTY") or _txt(ie, "ACTUALQTY"))
        hsn, rate, base_unit = masters.item_gst(nm, vdate)
        item_rows.append([nm, hsn or _txt(ie, "GSTHSNNAME"), to_uqc(unit or base_unit), qty,
                          rate, abs(to_float(_txt(ie, "AMOUNT")))])

    gst_amt = d.igst + d.cgst + d.sgst
    inferred = None
    if d.taxable:
        raw = gst_amt / d.taxable * 100
        inferred, dist = snap_rate(raw)
        if dist > 0.5 and not (item_rows and all(r[4] is not None for r in item_rows)):
            d.warnings.append(f"Effective GST rate {raw:.2f}% – mixed rates? set item GST rates in Tally")
    for r in item_rows:
        if r[4] is None:
            r[4] = inferred if inferred is not None else 0

    item_total = sum(r[5] for r in item_rows)
    if item_rows and item_total:
        scale = d.taxable / item_total if d.taxable else 1
        weights = [r[5] * scale * (r[4] or 0) for r in item_rows]
        wsum = sum(weights)
        for r, w in zip(item_rows, weights):
            share = (w / wsum) if wsum else (r[5] / item_total)
            d.items.append(ItemLine(name=r[0], hsn=r[1], uqc=r[2], qty=r[3], rate=r[4],
                                    taxable=r2(r[5] * scale), igst=r2(d.igst * share),
                                    cgst=r2(d.cgst * share), sgst=r2(d.sgst * share),
                                    cess=r2(d.cess * share)))
        by_rate = defaultdict(lambda: RateLine(0))
        for it in d.items:
            rl = by_rate[it.rate]
            rl.rate = it.rate
            rl.taxable += it.taxable
            rl.igst += it.igst
            rl.cgst += it.cgst
            rl.sgst += it.sgst
            rl.cess += it.cess
        d.rate_lines = [RateLine(rl.rate, r2(rl.taxable), r2(rl.igst), r2(rl.cgst), r2(rl.sgst),
                                 r2(rl.cess)) for rl in by_rate.values()]
    else:
        d.rate_lines = [RateLine(inferred or 0, d.taxable, d.igst, d.cgst, d.sgst, d.cess)]

    supplier_state = company_state if direction == "out" else (d.gstin[:2] if d.gstin else "")
    if supplier_state and d.pos and (d.igst or d.cgst or d.sgst):
        inter = d.pos != supplier_state if direction == "out" else supplier_state != company_state
        if inter and (d.cgst or d.sgst):
            d.warnings.append("Inter-state supply but CGST/SGST charged")
        if not inter and d.igst:
            d.warnings.append("Intra-state supply but IGST charged")
    if direction == "out" and d.taxable and not gst_amt:
        d.warnings.append("No GST charged (nil/exempt/export?)")
    return d


def vouchers_to_docs(roots, masters: TallyMasters, company_gstin: str, d_from=None, d_to=None,
                     progress=None):
    company_state = clean_gstin(company_gstin)[:2]
    out_docs, in_docs = [], []
    seen = 0
    for root in roots:
        for v in root.iter("VOUCHER"):
            seen += 1
            if progress and seen % 5000 == 0:
                progress(f"  …{seen:,} vouchers read")
            if _yes(_txt(v, "ISCANCELLED")) or _yes(_txt(v, "ISOPTIONAL")) or _yes(_txt(v, "ISDELETED")):
                continue
            vd = parse_date(_txt(v, "DATE"))
            if d_from and vd and not (d_from <= vd <= d_to):
                continue
            doc = voucher_to_doc(v, masters, company_state)
            if doc is None or (not doc.taxable and not doc.tax):
                continue
            (out_docs if doc.direction == "out" else in_docs).append(doc)
    return out_docs, in_docs


def _month_chunks(d_from, d_to):
    out, cur = [], d_from
    while cur <= d_to:
        nxt = date(cur.year + (cur.month == 12), 1 if cur.month == 12 else cur.month + 1, 1)
        out.append((cur, min(d_to, nxt - timedelta(days=1))))
        cur = nxt
    return out


def fetch_masters(client: TallyClient, company, cache_dir=None, progress=None):
    say = progress or (lambda m, f=None: None)
    say("Reading masters from Tally (ledgers, groups, voucher types, stock items)…")
    master_raws = client.fetch_masters_raw(company)
    masters = TallyMasters()
    for raw in master_raws:
        masters.load(parse_xml(raw))
    say(f"  {len(masters.ledgers):,} ledgers, {len(masters.items):,} stock items")
    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)
        for i, raw in enumerate(master_raws):
            with open(os.path.join(cache_dir, f"masters_{i}.xml"), "wb") as f:
                f.write(raw)
    return masters


def fetch_months(client: TallyClient, company, company_gstin, months, masters=None, cache_dir=None,
                 progress=None):
    """Fetch the given months [(y, m), ...] one at a time. Returns (masters, {(y, m): (out_docs, in_docs)}).
    progress(message, fraction_done_or_None)."""
    say = progress or (lambda m, f=None: None)
    steps = len(months) + (0 if masters else 1)
    done = 0
    if masters is None:
        say("Connecting to Tally…", 0.02)
        masters = fetch_masters(client, company, cache_dir, progress)
        done += 1
    result = {}
    for (y, m) in months:
        a = date(y, m, 1)
        b = date(y + (m == 12), 1 if m == 12 else m + 1, 1) - timedelta(days=1)
        say(f"Reading vouchers {a:%b-%Y} from Tally…", done / steps)
        raw = client.fetch_vouchers_raw(company, a, b)
        if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)
            with open(os.path.join(cache_dir, f"vouchers_{a:%Y%m}.xml"), "wb") as f:
                f.write(raw)
        say(f"Processing {a:%b-%Y}…", (done + 0.5) / steps)
        o, i = vouchers_to_docs([parse_xml(raw)], masters, company_gstin, a, b, progress)
        say(f"  {a:%b-%Y}: {len(o):,} sales docs, {len(i):,} purchase docs")
        result[(y, m)] = (o, i)
        done += 1
    say("Tally data loaded.", 1.0)
    return masters, result


def fetch_from_tally(client: TallyClient, company, company_gstin, d_from, d_to, cache_dir=None, progress=None):
    """Convenience wrapper: whole period, returns (out_docs, in_docs)."""
    months = [(a.year, a.month) for a, _ in _month_chunks(d_from, d_to)]
    _, res = fetch_months(client, company, company_gstin, months, None, cache_dir, progress)
    out_docs, in_docs = [], []
    for ym in months:
        out_docs += res[ym][0]
        in_docs += res[ym][1]
    return out_docs, in_docs


def load_from_files(paths, company_gstin, d_from=None, d_to=None, progress=None):
    """Load Tally XML exports (Day Book / masters, or files cached by fetch_from_tally)."""
    say = progress or (lambda m, f=None: None)
    roots = []
    with_vouchers = []
    for n, p in enumerate(paths):
        say(f"Reading {os.path.basename(p)} ({os.path.getsize(p) / 1e6:,.1f} MB)…", n / (len(paths) + 1))
        with open(p, "rb") as f:
            root = parse_xml(f.read())
        roots.append(root)
        if root.find(".//VOUCHER") is not None:
            with_vouchers.append(root)
    masters = TallyMasters()
    for root in roots:
        masters.load(root)
    say("Converting vouchers…", len(paths) / (len(paths) + 1))
    return vouchers_to_docs(with_vouchers, masters, company_gstin, d_from, d_to, progress)
