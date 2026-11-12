"""Generate sample Tally XML (masters + vouchers) and a GSTR-2B JSON for testing.
Usage: python tests/make_sample.py <out_dir>"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from gstreco.normalize import gstin_checksum  # noqa: E402


def gstin(state, pan, n="1"):
    g14 = f"{state}{pan}{n}Z"
    return g14 + gstin_checksum(g14)


COMPANY = gstin("29", "AABCT1234F")
SUP_A = gstin("29", "AAACA1111A")   # intra-state supplier
SUP_B = gstin("27", "AAACB2222B")   # inter-state supplier
SUP_C = gstin("29", "AAACC3333C")
SUP_D = gstin("33", "AAACD4444D")
CUST_X = gstin("29", "AAACX5555X")
CUST_Y = gstin("07", "AAACY6666Y")

MASTERS = f"""<ENVELOPE><BODY><DATA><COLLECTION>
<GROUP NAME="Duties &amp; Taxes"><PARENT>&#4; Primary</PARENT></GROUP>
<GROUP NAME="Sundry Debtors"><PARENT>&#4; Primary</PARENT></GROUP>
<GROUP NAME="Sundry Creditors"><PARENT>&#4; Primary</PARENT></GROUP>
<GROUP NAME="Purchase Accounts"><PARENT>&#4; Primary</PARENT></GROUP>
<GROUP NAME="Sales Accounts"><PARENT>&#4; Primary</PARENT></GROUP>
<LEDGER NAME="Input CGST"><PARENT>Duties &amp; Taxes</PARENT><GSTDUTYHEAD>Central Tax</GSTDUTYHEAD><TAXTYPE>GST</TAXTYPE></LEDGER>
<LEDGER NAME="Input SGST"><PARENT>Duties &amp; Taxes</PARENT><GSTDUTYHEAD>State Tax</GSTDUTYHEAD><TAXTYPE>GST</TAXTYPE></LEDGER>
<LEDGER NAME="Input IGST"><PARENT>Duties &amp; Taxes</PARENT><GSTDUTYHEAD>Integrated Tax</GSTDUTYHEAD><TAXTYPE>GST</TAXTYPE></LEDGER>
<LEDGER NAME="Output CGST"><PARENT>Duties &amp; Taxes</PARENT><GSTDUTYHEAD>Central Tax</GSTDUTYHEAD></LEDGER>
<LEDGER NAME="Output SGST"><PARENT>Duties &amp; Taxes</PARENT><GSTDUTYHEAD>State Tax</GSTDUTYHEAD></LEDGER>
<LEDGER NAME="Output IGST"><PARENT>Duties &amp; Taxes</PARENT><GSTDUTYHEAD>Integrated Tax</GSTDUTYHEAD></LEDGER>
<LEDGER NAME="TDS on Contract"><PARENT>Duties &amp; Taxes</PARENT><TAXTYPE>TDS</TAXTYPE></LEDGER>
<LEDGER NAME="Purchase GST"><PARENT>Purchase Accounts</PARENT></LEDGER>
<LEDGER NAME="Sales GST"><PARENT>Sales Accounts</PARENT></LEDGER>
<LEDGER NAME="Round Off"><PARENT>Indirect Expenses</PARENT></LEDGER>
<LEDGER NAME="Alpha Traders"><PARENT>Sundry Creditors</PARENT><PARTYGSTIN>{SUP_A}</PARTYGSTIN></LEDGER>
<LEDGER NAME="Beta Industries"><PARENT>Sundry Creditors</PARENT><LEDGSTREGDETAILS.LIST><GSTIN>{SUP_B}</GSTIN></LEDGSTREGDETAILS.LIST></LEDGER>
<LEDGER NAME="Gamma Supplies"><PARENT>Sundry Creditors</PARENT><PARTYGSTIN>{SUP_C}</PARTYGSTIN></LEDGER>
<LEDGER NAME="Delta Chem"><PARENT>Sundry Creditors</PARENT><PARTYGSTIN>{SUP_D}</PARTYGSTIN></LEDGER>
<LEDGER NAME="Xeno Retail"><PARENT>Sundry Debtors</PARENT><PARTYGSTIN>{CUST_X}</PARTYGSTIN></LEDGER>
<LEDGER NAME="Yotta Pvt Ltd"><PARENT>Sundry Debtors</PARENT><PARTYGSTIN>{CUST_Y}</PARTYGSTIN></LEDGER>
<LEDGER NAME="Cash Customer"><PARENT>Sundry Debtors</PARENT></LEDGER>
<VOUCHERTYPE NAME="Sales"><PARENT>Sales</PARENT></VOUCHERTYPE>
<VOUCHERTYPE NAME="GST Sales"><PARENT>Sales</PARENT></VOUCHERTYPE>
<VOUCHERTYPE NAME="Purchase"><PARENT>Purchase</PARENT></VOUCHERTYPE>
<VOUCHERTYPE NAME="Credit Note"><PARENT>Credit Note</PARENT></VOUCHERTYPE>
<VOUCHERTYPE NAME="Debit Note"><PARENT>Debit Note</PARENT></VOUCHERTYPE>
<STOCKITEM NAME="Widget"><BASEUNITS>Nos</BASEUNITS>
 <GSTDETAILS.LIST><APPLICABLEFROM>20250401</APPLICABLEFROM><HSNCODE>84719000</HSNCODE>
  <STATEWISEDETAILS.LIST><RATEDETAILS.LIST><GSTRATEDUTYHEAD>Integrated Tax</GSTRATEDUTYHEAD><GSTRATE>18</GSTRATE></RATEDETAILS.LIST></STATEWISEDETAILS.LIST></GSTDETAILS.LIST></STOCKITEM>
<STOCKITEM NAME="Gadget"><BASEUNITS>Kgs</BASEUNITS>
 <GSTDETAILS.LIST><APPLICABLEFROM>20250401</APPLICABLEFROM><HSNCODE>39269099</HSNCODE>
  <STATEWISEDETAILS.LIST><RATEDETAILS.LIST><GSTRATEDUTYHEAD>Integrated Tax</GSTRATEDUTYHEAD><GSTRATE>5</GSTRATE></RATEDETAILS.LIST></STATEWISEDETAILS.LIST></GSTDETAILS.LIST></STOCKITEM>
</COLLECTION></DATA></BODY></ENVELOPE>"""


def le(name, amt):
    return f"<ALLLEDGERENTRIES.LIST><LEDGERNAME>{name}</LEDGERNAME><AMOUNT>{amt:.2f}</AMOUNT></ALLLEDGERENTRIES.LIST>"


def purchase(vno, date, party, ref, refdate, taxable, igst=0, cgst=0, extra=""):
    total = taxable + igst + 2 * cgst
    body = le(party, total) + le("Purchase GST", -taxable)
    if igst:
        body += le("Input IGST", -igst)
    if cgst:
        body += le("Input CGST", -cgst) + le("Input SGST", -cgst)
    return (f"<VOUCHER VCHTYPE=\"Purchase\"><DATE>{date}</DATE><VOUCHERTYPENAME>Purchase</VOUCHERTYPENAME>"
            f"<VOUCHERNUMBER>{vno}</VOUCHERNUMBER><PARTYLEDGERNAME>{party}</PARTYLEDGERNAME>"
            f"<REFERENCE>{ref}</REFERENCE><REFERENCEDATE>{refdate}</REFERENCEDATE>{extra}{body}</VOUCHER>")


def sales_items(vno, date, party, pos, items, vt="GST Sales", inter=False):
    """items: [(name, qty, unit, amount, rate)]"""
    inv = ""
    taxable = tax = 0
    for name, qty, unit, amt, rate in items:
        inv += (f"<ALLINVENTORYENTRIES.LIST><STOCKITEMNAME>{name}</STOCKITEMNAME>"
                f"<BILLEDQTY> {qty} {unit}</BILLEDQTY><AMOUNT>{amt:.2f}</AMOUNT>"
                f"<ACCOUNTINGALLOCATIONS.LIST><LEDGERNAME>Sales GST</LEDGERNAME><AMOUNT>{amt:.2f}</AMOUNT>"
                f"</ACCOUNTINGALLOCATIONS.LIST></ALLINVENTORYENTRIES.LIST>")
        taxable += amt
        tax += round(amt * rate / 100, 2)
    body = le(party, -(taxable + tax))
    if inter:
        body += le("Output IGST", tax)
    else:
        body += le("Output CGST", tax / 2) + le("Output SGST", tax / 2)
    return (f"<VOUCHER VCHTYPE=\"{vt}\"><DATE>{date}</DATE><VOUCHERTYPENAME>{vt}</VOUCHERTYPENAME>"
            f"<VOUCHERNUMBER>{vno}</VOUCHERNUMBER><PARTYLEDGERNAME>{party}</PARTYLEDGERNAME>"
            f"<PLACEOFSUPPLY>{pos}</PLACEOFSUPPLY>{body}{inv}</VOUCHER>")


VOUCHERS = "<ENVELOPE><BODY><DATA><COLLECTION>" + "".join([
    purchase("P1", "20261003", "Alpha Traders", "AT/001/26-27", "20261002", 10000, cgst=900),
    purchase("P2", "20261005", "Beta Industries", "BI-552", "20261005", 50000, igst=9000),
    purchase("P3", "20261008", "Gamma Supplies", "GS 0098", "20261007", 20000, cgst=1800),   # 2B: GS/98 -> probable
    purchase("P4", "20261012", "Alpha Traders", "AT/002/26-27", "20261012", 15000, cgst=1350),  # 2B amount differs
    purchase("P5", "20261015", "Delta Chem", "DC-77", "20261015", 8000, igst=1440),            # not in 2B
    purchase("P6", "20261020", "Beta Industries", "BI-560", "20261020", 30000, igst=5400,
             extra=le("TDS on Contract", 600)),
    "<VOUCHER><DATE>20261021</DATE><VOUCHERTYPENAME>Purchase</VOUCHERTYPENAME><ISCANCELLED>Yes</ISCANCELLED>"
    "<VOUCHERNUMBER>P7</VOUCHERNUMBER></VOUCHER>",
    sales_items("S/001", "20261004", "Xeno Retail", "Karnataka", [("Widget", 10, "Nos", 10000, 18)]),
    sales_items("S/002", "20261009", "Yotta Pvt Ltd", "Delhi",
                [("Widget", 5, "Nos", 5000, 18), ("Gadget", 20, "Kgs", 4000, 5)], inter=True),
    sales_items("S/003", "20261011", "Cash Customer", "Karnataka", [("Gadget", 3, "Kgs", 1500, 5)]),
    sales_items("S/004", "20261018", "Cash Customer", "Maharashtra", [("Widget", 150, "Nos", 150000, 18)],
                inter=True),
    sales_items("S/005", "20261025", "Cash Customer", "Tamil Nadu", [("Widget", 2, "Nos", 2000, 18)],
                inter=True),
    sales_items("CN/01", "20261028", "Xeno Retail", "Karnataka", [("Widget", 1, "Nos", 1000, 18)],
                vt="Credit Note"),
    sales_items("S/999", "20260930", "Xeno Retail", "Karnataka", [("Widget", 1, "Nos", 1000, 18)]),  # outside period
]) + "</COLLECTION></DATA></BODY></ENVELOPE>"


def inv2b(inum, dt, val, txval, igst=0, cgst=0, itcavl="Y", rev="N", pos="29"):
    return {"inum": inum, "dt": dt, "val": val, "typ": "R", "pos": pos, "rev": rev, "itcavl": itcavl,
            "rsn": "" if itcavl == "Y" else "P", "txval": txval, "igst": igst, "cgst": cgst, "sgst": cgst,
            "cess": 0, "items": [{"num": 1, "rt": 18, "txval": txval, "igst": igst, "cgst": cgst,
                                  "sgst": cgst, "cess": 0}]}


GSTR2B = {"chksum": "x", "data": {"gstin": COMPANY, "rtnprd": "102026", "version": "1.0", "gendt": "14-11-2026",
    "docdata": {
        "b2b": [
            {"ctin": SUP_A, "trdnm": "ALPHA TRADERS", "inv": [
                inv2b("AT/001/26-27", "02-10-2026", 11800, 10000, cgst=900),
                inv2b("AT/002/26-27", "12-10-2026", 17110, 14500, cgst=1305)]},
            {"ctin": SUP_B, "trdnm": "BETA INDUSTRIES", "inv": [
                inv2b("BI-552", "05-10-2026", 59000, 50000, igst=9000, pos="29"),
                inv2b("BI-560", "20-10-2026", 35400, 30000, igst=5400)]},
            {"ctin": SUP_C, "trdnm": "GAMMA SUPPLIES", "inv": [
                inv2b("GS/98/26-27", "07-10-2026", 23600, 20000, cgst=1800),
                inv2b("GS/105", "29-10-2026", 5900, 5000, cgst=450, itcavl="N")]},
            {"ctin": gstin("29", "AAACE7777E"), "trdnm": "EPSILON TRANSPORT", "inv": [
                inv2b("ET-12", "15-10-2026", 10500, 10000, cgst=250, rev="Y")]},
        ],
        "cdnr": [{"ctin": SUP_A, "trdnm": "ALPHA TRADERS", "nt": [
            {"ntnum": "ACN-3", "dt": "25-10-2026", "typ": "C", "val": 1180, "pos": "29", "rev": "N",
             "itcavl": "Y", "txval": 1000, "igst": 0, "cgst": 90, "sgst": 90, "cess": 0}]}],
    }}}


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "sample"
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "tally_masters.xml"), "w", encoding="utf-8") as f:
        f.write(MASTERS)
    with open(os.path.join(out, "tally_vouchers.xml"), "w", encoding="utf-8") as f:
        f.write(VOUCHERS)
    with open(os.path.join(out, "GSTR2B_102026.json"), "w", encoding="utf-8") as f:
        json.dump(GSTR2B, f, indent=1)
    with open(os.path.join(out, "company_gstin.txt"), "w") as f:
        f.write(COMPANY)
    print("Sample written to", out, "company GSTIN", COMPANY)
