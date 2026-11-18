from dataclasses import dataclass, field
from datetime import date
from typing import List, Optional


@dataclass
class RateLine:
    rate: float
    taxable: float = 0.0
    igst: float = 0.0
    cgst: float = 0.0
    sgst: float = 0.0
    cess: float = 0.0


@dataclass
class ItemLine:
    name: str
    hsn: str
    uqc: str
    qty: float
    rate: float
    taxable: float = 0.0
    igst: float = 0.0
    cgst: float = 0.0
    sgst: float = 0.0
    cess: float = 0.0


@dataclass
class Doc:
    """One GST document (invoice / credit note / debit note) from books or portal."""
    source: str                 # 'books', '2B', 'GSTR1'
    direction: str              # 'out' (sales) or 'in' (purchase)
    doc_type: str               # 'INV', 'CN', 'DN'
    gstin: str = ""
    party: str = ""
    inv_no: str = ""
    inv_date: Optional[date] = None
    taxable: float = 0.0
    igst: float = 0.0
    cgst: float = 0.0
    sgst: float = 0.0
    cess: float = 0.0
    total: float = 0.0
    pos: str = ""               # place of supply state code, e.g. '29'
    rcm: bool = False
    itc_avail: str = ""         # 2B: 'Y' / 'N' / 'T'
    itc_reason: str = ""
    voucher_no: str = ""
    voucher_type: str = ""
    note: str = ""              # free-text origin note (e.g. 'b2ba amended')
    rate_lines: List[RateLine] = field(default_factory=list)
    items: List[ItemLine] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def tax(self) -> float:
        return round(self.igst + self.cgst + self.sgst + self.cess, 2)

    @property
    def is_note(self) -> bool:
        return self.doc_type in ("CN", "DN")

    @property
    def sign(self) -> int:
        """+1 for invoices / debit notes, -1 for credit notes (for netting)."""
        return -1 if self.doc_type == "CN" else 1
