"""Parsers for files downloaded from the GST portal: GSTR-2B JSON (or ZIP) and GSTR-1 JSON."""
import json
import zipfile
from collections import defaultdict

from .models import Doc, RateLine
from .normalize import clean_gstin, parse_date, r2


def _load_jsons(path):
    if zipfile.is_zipfile(path):
        out = []
        with zipfile.ZipFile(path) as z:
            for n in z.namelist():
                if n.lower().endswith(".json"):
                    out.append(json.loads(z.read(n).decode("utf-8-sig")))
        return out
    with open(path, "r", encoding="utf-8-sig") as f:
        return [json.load(f)]


def _amounts_2b(obj):
    """2B invoice/note: amounts at doc level, else sum items."""
    items = obj.get("items") or []
    rate_lines = [RateLine(float(i.get("rt", 0) or 0), r2(i.get("txval")), r2(i.get("igst")),
                           r2(i.get("cgst")), r2(i.get("sgst")), r2(i.get("cess"))) for i in items]
    if "txval" in obj:
        vals = [r2(obj.get(k)) for k in ("txval", "igst", "cgst", "sgst", "cess")]
    else:
        vals = [r2(sum(getattr(rl, a) for rl in rate_lines))
                for a in ("taxable", "igst", "cgst", "sgst", "cess")]
    return vals, rate_lines


def _doc_2b(ctin, trdnm, obj, doc_type, num_key, note=""):
    (tx, ig, cg, sg, cs), rls = _amounts_2b(obj)
    return Doc(source="2B", direction="in", doc_type=doc_type, gstin=clean_gstin(ctin),
               party=trdnm or "", inv_no=str(obj.get(num_key, "")), inv_date=parse_date(obj.get("dt")),
               taxable=tx, igst=ig, cgst=cg, sgst=sg, cess=cs, total=r2(obj.get("val")),
               pos=str(obj.get("pos", "") or ""), rcm=str(obj.get("rev", "N")).upper() == "Y",
               itc_avail=str(obj.get("itcavl", "") or ""), itc_reason=str(obj.get("rsn", "") or ""),
               note=note, rate_lines=rls)


def load_gstr2b(paths):
    """Returns (docs, meta). Accepts one path or a list (e.g. several months)."""
    if isinstance(paths, str):
        paths = [paths]
    docs, meta = [], {"gstin": "", "periods": []}
    for p in paths:
        for js in _load_jsons(p):
            d = js.get("data", js)
            meta["gstin"] = d.get("gstin", meta["gstin"])
            if d.get("rtnprd"):
                meta["periods"].append(d["rtnprd"])
            dd = d.get("docdata", {})
            for sup in dd.get("b2b", []):
                for inv in sup.get("inv", []):
                    docs.append(_doc_2b(sup.get("ctin"), sup.get("trdnm"), inv, "INV", "inum"))
            for sup in dd.get("b2ba", []):
                for inv in sup.get("inv", []):
                    docs.append(_doc_2b(sup.get("ctin"), sup.get("trdnm"), inv, "INV", "inum",
                                        f"Amended (b2ba) from {inv.get('oinum', '')}"))
            for key in ("cdnr", "cdnra"):
                for sup in dd.get(key, []):
                    for nt in sup.get("nt", []):
                        dt = "CN" if str(nt.get("typ", "C")).upper() == "C" else "DN"
                        docs.append(_doc_2b(sup.get("ctin"), sup.get("trdnm"), nt, dt, "ntnum",
                                            "Amended (cdnra)" if key == "cdnra" else ""))
    return docs, meta


# ---------------------------------------------------------------- GSTR-1
def _itms_r1(itms):
    rls = []
    for it in itms or []:
        d = it.get("itm_det", it)
        rls.append(RateLine(float(d.get("rt", 0) or 0), r2(d.get("txval")), r2(d.get("iamt")),
                            r2(d.get("camt")), r2(d.get("samt")), r2(d.get("csamt"))))
    return rls


def _doc_r1(ctin, obj, doc_type, num_key, date_key, pos=""):
    rls = _itms_r1(obj.get("itms"))
    d = Doc(source="GSTR1", direction="out", doc_type=doc_type, gstin=clean_gstin(ctin),
            inv_no=str(obj.get(num_key, "")), inv_date=parse_date(obj.get(date_key)),
            total=r2(obj.get("val")), pos=str(obj.get("pos", pos) or ""), rate_lines=rls,
            rcm=str(obj.get("rchrg", "N")).upper() == "Y")
    for attr in ("taxable", "igst", "cgst", "sgst", "cess"):
        setattr(d, attr, r2(sum(getattr(rl, attr) for rl in rls)))
    return d


def load_gstr1(path):
    """Returns (docs, b2cs_rows, meta). Works for the JSON this app builds and for the
    'filed GSTR-1' JSON downloaded from the portal's Prepare Offline > Download tab."""
    docs, b2cs = [], []
    meta = {}
    for js in _load_jsons(path):
        d = js.get("data", js)
        meta = {"gstin": d.get("gstin", ""), "fp": d.get("fp", "")}
        for sup in d.get("b2b", []):
            for inv in sup.get("inv", []):
                docs.append(_doc_r1(sup.get("ctin"), inv, "INV", "inum", "idt"))
        for grp in d.get("b2cl", []):
            for inv in grp.get("inv", []):
                docs.append(_doc_r1("", inv, "INV", "inum", "idt", grp.get("pos", "")))
        for sup in d.get("cdnr", []):
            for nt in sup.get("nt", []):
                dt = "CN" if str(nt.get("ntty", "C")).upper() == "C" else "DN"
                docs.append(_doc_r1(sup.get("ctin"), nt, dt, "nt_num", "nt_dt"))
        for nt in d.get("cdnur", []):
            dt = "CN" if str(nt.get("ntty", "C")).upper() == "C" else "DN"
            docs.append(_doc_r1("", nt, dt, "nt_num", "nt_dt"))
        for row in d.get("b2cs", []):
            b2cs.append({"pos": str(row.get("pos", "")), "rt": float(row.get("rt", 0) or 0),
                         "txval": r2(row.get("txval")), "iamt": r2(row.get("iamt")),
                         "camt": r2(row.get("camt")), "samt": r2(row.get("samt")),
                         "csamt": r2(row.get("csamt"))})
    return docs, b2cs, meta


def aggregate_b2cs(rows):
    agg = defaultdict(lambda: defaultdict(float))
    for r in rows:
        k = (r["pos"], r["rt"])
        for f in ("txval", "iamt", "camt", "samt", "csamt"):
            agg[k][f] += r[f]
    return {k: {f: r2(v) for f, v in d.items()} for k, d in agg.items()}
