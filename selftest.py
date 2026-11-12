"""End-to-end test on sample data (no Tally / portal needed).
Usage: python tests/selftest.py"""
import os
import sys
import tempfile

# Windows consoles/CI pipes default to cp1252, which cannot print some characters used in labels.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import make_sample  # noqa: E402
from gstreco import gstr1_builder, gstr3b, portal_data, reco, report, tally  # noqa: E402
from gstreco.normalize import is_valid_gstin, period_bounds  # noqa: E402


def main():
    out = tempfile.mkdtemp(prefix="gstreco_test_")
    make_sample.write_sample(out)
    gstin = make_sample.COMPANY
    assert is_valid_gstin(gstin)
    d_from, d_to = period_bounds((2026, 10), (2026, 10))

    out_docs, in_docs = tally.load_from_files(
        [os.path.join(out, "tally_masters.xml"), os.path.join(out, "tally_vouchers.xml")], gstin, d_from, d_to)
    print(f"Books: {len(out_docs)} outward, {len(in_docs)} inward")
    for d in out_docs + in_docs:
        print(f"  {d.direction} {d.doc_type} {d.inv_no:14} {d.gstin:16} pos={d.pos} tx={d.taxable:>10} "
              f"I={d.igst} C={d.cgst} S={d.sgst} rates={[rl.rate for rl in d.rate_lines]} {d.warnings}")
    assert len(in_docs) == 6, "cancelled voucher must be skipped"
    assert len(out_docs) == 6, "out-of-period voucher must be skipped"
    p6 = next(d for d in in_docs if d.voucher_no == "P6")
    assert p6.taxable == 30000, "TDS ledger must not inflate taxable value"

    portal, meta = portal_data.load_gstr2b(os.path.join(out, "GSTR2B_102026.json"))
    rows = reco.reconcile(in_docs, portal, tol=1.0)
    print("\nPurchase reco:")
    for r in rows:
        print(f"  {r.status:16} {(r.books or r.portal).inv_no:14} {r.remarks}")
    st = {(r.books or r.portal).inv_no: r.status for r in rows}
    assert st["AT/001/26-27"] == reco.MATCHED
    assert st["GS 0098"] == reco.PROBABLE
    assert st["AT/002/26-27"] == reco.MISMATCH
    assert st["DC-77"] == reco.BOOKS_ONLY
    assert st["GS/105"] == reco.PORTAL_ONLY
    for s in reco.summarize(rows):
        print("  ", s)

    month_docs = [d for d in out_docs]
    data, warns, summ = gstr1_builder.build_gstr1(month_docs, gstin, "102026")
    print("\nGSTR-1 sections:", [k for k in data if k not in ("gstin", "fp", "version", "hash")])
    print("Summary:", summ)
    print("Warnings:", warns)
    assert len(data["b2b"]) == 2 and "b2cl" in data and "b2cs" in data and "cdnr" in data
    yotta = next(x for x in data["b2b"] if x["ctin"] == make_sample.CUST_Y)["inv"][0]
    assert sorted(i["itm_det"]["rt"] for i in yotta["itms"]) == [5, 18], "mixed-rate split"
    r1_path = gstr1_builder.save_gstr1(data, os.path.join(out, "GSTR1_102026.json"))

    p_docs, p_b2cs, _ = portal_data.load_gstr1(r1_path)
    inv_books = [d for d in out_docs if gstr1_builder.section_of(d, gstin) not in ("B2CS", "NIL")]
    srows = reco.reconcile(inv_books, p_docs)
    assert all(r.status == reco.MATCHED for r in srows), [(r.status, r.remarks) for r in srows]
    b2 = reco.reconcile_b2cs(gstr1_builder.books_b2cs(out_docs, gstin), portal_data.aggregate_b2cs(p_b2cs))
    assert all(r["status"] == reco.MATCHED for r in b2)
    print("\nSales vs own GSTR-1 JSON round-trip: all matched")

    rows3b = gstr3b.compute_3b(out_docs, in_docs, portal, rows, gstin)
    print("\nGSTR-3B:")
    for sec, label, v in rows3b:
        print(f"  {sec:8} {label[:60]:60} {v}")

    report.write_reco(os.path.join(out, "purchase_reco.xlsx"), rows, "Purchase reco Oct-2026", portal_label="2B")
    report.write_reco(os.path.join(out, "sales_reco.xlsx"), srows, "Sales reco", b2, "GSTR-1")
    report.write_3b(os.path.join(out, "gstr3b.xlsx"), rows3b, "GSTR-3B Oct-2026")
    report.write_books(os.path.join(out, "books.xlsx"), out_docs + in_docs, "Books")
    print("\nALL TESTS PASSED. Output in", out)
    return out


if __name__ == "__main__":
    main()
