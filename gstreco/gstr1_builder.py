"""Build GSTR-1 JSON (offline-tool / portal upload format) from Tally sales documents."""
import json
from collections import defaultdict

from .models import Doc
from .normalize import clean_gstin, fmt_date, is_valid_gstin, r2

B2CL_LIMIT = 100000.0      # inter-state B2C invoice value threshold (Rs 1 lakh, w.e.f. Aug-2024)
JSON_VERSION = "GST3.2.1"


def _itm(rl, inter, num):
    det = {"rt": rl.rate, "txval": r2(rl.taxable)}
    if inter:
        det["iamt"] = r2(rl.igst)
    else:
        det["camt"] = r2(rl.cgst)
        det["samt"] = r2(rl.sgst)
    det["csamt"] = r2(rl.cess)
    return {"num": num, "itm_det": det}


def _itms(doc, inter):
    out, seen = [], set()
    for rl in doc.rate_lines:
        num = int(round(rl.rate * 100)) + 1
        while num in seen:
            num += 1
        seen.add(num)
        out.append(_itm(rl, inter, num))
    return out


def build_gstr1(docs, company_gstin, fp, include_hsn=True):
    """docs: outward books docs for ONE month. fp: 'MMYYYY'. Returns (json_dict, warnings, summary)."""
    company_gstin = clean_gstin(company_gstin)
    home = company_gstin[:2]
    warnings = []
    b2b, cdnr = defaultdict(list), defaultdict(list)
    b2cl = defaultdict(list)
    cdnur = []
    b2cs = defaultdict(lambda: defaultdict(float))
    hsn = {"b2b": defaultdict(lambda: defaultdict(float)), "b2c": defaultdict(lambda: defaultdict(float))}
    no_hsn = 0
    summary = defaultdict(lambda: [0, 0.0, 0.0])

    def add_summary(sec, d):
        summary[sec][0] += 1
        summary[sec][1] += d.sign * d.taxable
        summary[sec][2] += d.sign * d.tax

    for d in docs:
        if d.direction != "out":
            continue
        if not d.tax and d.taxable:
            warnings.append(f"{d.inv_no}: no tax – nil/exempt/export not exported to JSON, enter in Table 8/6A")
            continue
        pos = d.pos or (d.gstin[:2] if d.gstin else home)
        inter = pos != home
        registered = is_valid_gstin(d.gstin)
        if d.gstin and not registered:
            warnings.append(f"{d.inv_no}: invalid GSTIN {d.gstin} – treated as B2C")
        for w in d.warnings:
            warnings.append(f"{d.inv_no}: {w}")
        base = {"val": r2(d.total), "pos": pos}
        if d.doc_type == "INV":
            if registered:
                b2b[d.gstin].append({"inum": d.inv_no, "idt": fmt_date(d.inv_date), **base,
                                     "rchrg": "N", "inv_typ": "R", "itms": _itms(d, inter)})
                add_summary("B2B", d)
            elif inter and d.total > B2CL_LIMIT:
                b2cl[pos].append({"inum": d.inv_no, "idt": fmt_date(d.inv_date), "val": r2(d.total),
                                  "itms": _itms(d, True)})
                add_summary("B2CL", d)
            else:
                for rl in d.rate_lines:
                    a = b2cs[("INTER" if inter else "INTRA", pos, rl.rate)]
                    a["txval"] += rl.taxable
                    a["iamt"] += rl.igst
                    a["camt"] += rl.cgst
                    a["samt"] += rl.sgst
                    a["csamt"] += rl.cess
                add_summary("B2CS", d)
        else:  # CN / DN
            ntty = "C" if d.doc_type == "CN" else "D"
            if registered:
                cdnr[d.gstin].append({"ntty": ntty, "nt_num": d.inv_no, "nt_dt": fmt_date(d.inv_date),
                                      **base, "rchrg": "N", "inv_typ": "R", "itms": _itms(d, inter)})
                add_summary("CDNR", d)
            elif inter and d.total > B2CL_LIMIT:
                cdnur.append({"typ": "B2CL", "ntty": ntty, "nt_num": d.inv_no,
                              "nt_dt": fmt_date(d.inv_date), **base, "itms": _itms(d, True)})
                add_summary("CDNUR", d)
            else:
                s = -1 if ntty == "C" else 1
                for rl in d.rate_lines:
                    a = b2cs[("INTER" if inter else "INTRA", pos, rl.rate)]
                    for k, v in (("txval", rl.taxable), ("iamt", rl.igst), ("camt", rl.cgst),
                                 ("samt", rl.sgst), ("csamt", rl.cess)):
                        a[k] += s * v
                add_summary("B2CS (net of notes)", d)

        # HSN summary (credit notes reduce)
        bucket = hsn["b2b" if registered else "b2c"]
        if d.items:
            for it in d.items:
                if not it.hsn:
                    no_hsn += 1
                    continue
                h = bucket[(it.hsn, it.uqc, it.rate)]
                h["qty"] += d.sign * it.qty
                for k, v in (("txval", it.taxable), ("iamt", it.igst), ("camt", it.cgst),
                             ("samt", it.sgst), ("csamt", it.cess)):
                    h[k] += d.sign * v
        else:
            no_hsn += 1

    out = {"gstin": company_gstin, "fp": fp, "version": JSON_VERSION, "hash": "hash"}
    if b2b:
        out["b2b"] = [{"ctin": g, "inv": v} for g, v in sorted(b2b.items())]
    if b2cl:
        out["b2cl"] = [{"pos": p, "inv": v} for p, v in sorted(b2cl.items())]
    if b2cs:
        rows = []
        for (sply, pos, rt), a in sorted(b2cs.items()):
            row = {"sply_ty": sply, "pos": pos, "typ": "OE", "rt": rt, "txval": r2(a["txval"])}
            if sply == "INTER":
                row["iamt"] = r2(a["iamt"])
            else:
                row["camt"], row["samt"] = r2(a["camt"]), r2(a["samt"])
            row["csamt"] = r2(a["csamt"])
            if row["txval"] < 0:
                warnings.append(f"B2CS POS {pos} @ {rt}% is negative after credit notes – adjust manually")
            rows.append(row)
        out["b2cs"] = rows
    if cdnr:
        out["cdnr"] = [{"ctin": g, "nt": v} for g, v in sorted(cdnr.items())]
    if cdnur:
        out["cdnur"] = cdnur
    if include_hsn and (hsn["b2b"] or hsn["b2c"]):
        sec = {}
        for kind in ("b2b", "b2c"):
            lst = []
            for i, ((code, uqc, rt), a) in enumerate(sorted(hsn[kind].items()), start=1):
                lst.append({"num": i, "hsn_sc": code, "uqc": uqc, "qty": round(a["qty"], 3), "rt": rt,
                            "txval": r2(a["txval"]), "iamt": r2(a["iamt"]), "camt": r2(a["camt"]),
                            "samt": r2(a["samt"]), "csamt": r2(a["csamt"])})
            if lst:
                sec[f"hsn_{kind}"] = lst
        out["hsn"] = sec
    if no_hsn:
        warnings.append(f"{no_hsn} line(s) without HSN (accounting invoices or HSN missing in stock "
                        f"item) – add HSN summary rows in the offline tool")
    summ = [(k, v[0], r2(v[1]), r2(v[2])) for k, v in summary.items()]
    return out, warnings, summ


def save_gstr1(data, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    return path


def books_b2cs(docs, company_gstin):
    """Aggregate B2C small docs by (pos, rate) – for comparison with filed GSTR-1 B2CS."""
    data, _, _ = build_gstr1(docs, company_gstin, "000000", include_hsn=False)
    agg = defaultdict(lambda: defaultdict(float))
    for r in data.get("b2cs", []):
        a = agg[(r["pos"], r["rt"])]
        for f in ("txval", "iamt", "camt", "samt", "csamt"):
            a[f] += r.get(f, 0)
    return {k: dict(v) for k, v in agg.items()}


def section_of(d: Doc, company_gstin: str) -> str:
    """GSTR-1 table a books document falls in: B2B, B2CL, B2CS, CDNR, CDNUR or NIL."""
    home = clean_gstin(company_gstin)[:2]
    if not d.tax:
        return "NIL"
    pos = d.pos or (d.gstin[:2] if d.gstin else home)
    inter = pos != home
    if is_valid_gstin(d.gstin):
        return "B2B" if d.doc_type == "INV" else "CDNR"
    if inter and d.total > B2CL_LIMIT:
        return "B2CL" if d.doc_type == "INV" else "CDNUR"
    return "B2CS"
