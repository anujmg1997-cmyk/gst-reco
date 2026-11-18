"""Helpers: GSTIN validation, state codes, invoice-number normalisation, dates, periods."""
import re
from datetime import date, datetime, timedelta

STATE_CODES = {
    "01": "Jammu and Kashmir", "02": "Himachal Pradesh", "03": "Punjab", "04": "Chandigarh",
    "05": "Uttarakhand", "06": "Haryana", "07": "Delhi", "08": "Rajasthan", "09": "Uttar Pradesh",
    "10": "Bihar", "11": "Sikkim", "12": "Arunachal Pradesh", "13": "Nagaland", "14": "Manipur",
    "15": "Mizoram", "16": "Tripura", "17": "Meghalaya", "18": "Assam", "19": "West Bengal",
    "20": "Jharkhand", "21": "Odisha", "22": "Chhattisgarh", "23": "Madhya Pradesh", "24": "Gujarat",
    "26": "Dadra and Nagar Haveli and Daman and Diu", "27": "Maharashtra", "29": "Karnataka",
    "30": "Goa", "31": "Lakshadweep", "32": "Kerala", "33": "Tamil Nadu", "34": "Puducherry",
    "35": "Andaman and Nicobar Islands", "36": "Telangana", "37": "Andhra Pradesh", "38": "Ladakh",
    "96": "Foreign Country", "97": "Other Territory",
}


def _key(s: str) -> str:
    return re.sub(r"[^a-z]", "", str(s).lower().replace("&", "and"))


_NAME_TO_CODE = {_key(v): k for k, v in STATE_CODES.items()}
_NAME_TO_CODE.update({
    "orissa": "21", "pondicherry": "34", "uttaranchal": "05", "nctofdelhi": "07",
    "newdelhi": "07", "damananddiu": "26", "dadraandnagarhaveli": "26", "dadranagarhaveli": "26",
    "andamanandnicobar": "35", "andamanandnicobarisland": "35", "jandk": "01",
    "andhrapradeshnew": "37", "telengana": "36", "chattisgarh": "22",
})


def state_code(value) -> str:
    """'Karnataka' / '29' / '29-Karnataka' -> '29'. Returns '' if unknown."""
    if value is None:
        return ""
    v = str(value).strip()
    if not v:
        return ""
    m = re.match(r"^(\d{1,2})(\b|-|\s)", v + " ")
    if m and m.group(1).zfill(2) in STATE_CODES:
        return m.group(1).zfill(2)
    return _NAME_TO_CODE.get(_key(v), "")


# ---------------------------------------------------------------- GSTIN
_CH = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_GSTIN_SHAPE = re.compile(r"^[0-9]{2}[A-Z0-9]{13}$")


def clean_gstin(g) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(g or "").upper())


def gstin_checksum(first14: str) -> str:
    total = 0
    for i, c in enumerate(first14):
        v = _CH.index(c) * (2 if i % 2 else 1)
        total += v // 36 + v % 36
    return _CH[(36 - total % 36) % 36]


def is_valid_gstin(g) -> bool:
    g = clean_gstin(g)
    if not _GSTIN_SHAPE.match(g):
        return False
    return g[14] == gstin_checksum(g[:14])


# ---------------------------------------------------------------- invoice numbers
def norm_inv(s) -> str:
    """Upper-case, keep only A-Z0-9, drop leading zeros of every digit run.
    'INV/0012/26-27' -> 'INV122627';  'inv-12/26-27' -> 'INV122627'."""
    s = re.sub(r"[^A-Z0-9]", "", str(s or "").upper())
    return re.sub(r"(?<![0-9])0+(?=[0-9])", "", s)


_FY_PAT = re.compile(r"(20)?\d{2}[-/](20)?\d{2}")


def digit_core(s) -> str:
    """Main serial number of an invoice, ignoring financial-year tags like 26-27 / 2026-27."""
    s = _FY_PAT.sub(" ", str(s or ""))
    runs = [r.lstrip("0") or "0" for r in re.findall(r"\d+", s)]
    return max(runs, key=len) if runs else ""


# ---------------------------------------------------------------- numbers / dates
def to_float(s) -> float:
    if s is None:
        return 0.0
    if isinstance(s, (int, float)):
        return float(s)
    m = re.search(r"-?\d[\d,]*(\.\d+)?", str(s))
    return float(m.group(0).replace(",", "")) if m else 0.0


def r2(x) -> float:
    return round(float(x or 0) + 0.0, 2)


_DATE_FORMATS = ("%Y%m%d", "%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%b-%Y", "%d-%b-%y",
                 "%d.%m.%Y", "%d %b %Y", "%d-%m-%y", "%d/%m/%y")


def parse_date(s):
    if s is None or s == "":
        return None
    if isinstance(s, datetime):
        return s.date()
    if isinstance(s, date):
        return s
    s = str(s).strip()
    for f in _DATE_FORMATS:
        try:
            return datetime.strptime(s, f).date()
        except ValueError:
            continue
    return None


def fmt_date(d) -> str:
    return d.strftime("%d-%m-%Y") if d else ""


STD_RATES = [0, 0.1, 0.25, 1, 1.5, 3, 5, 6, 7.5, 12, 18, 28, 40]


def snap_rate(r: float):
    best = min(STD_RATES, key=lambda s: abs(s - r))
    return best, abs(best - r)


# ---------------------------------------------------------------- periods
MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
MONTH_FULL = ["January", "February", "March", "April", "May", "June", "July", "August",
              "September", "October", "November", "December"]


def months_between(ym_from, ym_to):
    """(2026,4),(2026,6) -> [(2026,4),(2026,5),(2026,6)]"""
    y, m = ym_from
    out = []
    while (y, m) <= tuple(ym_to):
        out.append((y, m))
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def month_end(y, m) -> date:
    nxt = date(y + (m == 12), 1 if m == 12 else m + 1, 1)
    return nxt - timedelta(days=1)


def period_bounds(ym_from, ym_to):
    return date(ym_from[0], ym_from[1], 1), month_end(*ym_to)


def fp(y, m) -> str:
    """Return period in GST format MMYYYY."""
    return f"{m:02d}{y}"


def fy_label(y, m) -> str:
    """Financial year label like '2026-27' for a month."""
    start = y if m >= 4 else y - 1
    return f"{start}-{str(start + 1)[2:]}"


def period_label(y, m) -> str:
    return f"{MONTH_ABBR[m - 1]}-{y}"


def parse_period_label(s):
    mon, yr = s.split("-")
    return int(yr), MONTH_ABBR.index(mon) + 1
