"""GST ReconX – desktop app (Windows). Tally <-> GST portal reconciliation.

Run:  python app.py              normal start
      python app.py --selfcheck DIR   build-time UI smoke test (sample data, screenshots into DIR, then exits)
"""
import os
import shutil
import sys
import tempfile

if "--selfcheck" in sys.argv:                       # must happen before gstreco.config is imported
    os.environ.setdefault("GSTRECONX_HOME", tempfile.mkdtemp(prefix="gstreconx_selfcheck_"))

import queue  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import traceback  # noqa: E402
from datetime import date  # noqa: E402

import tkinter as tk  # noqa: E402
from tkinter import filedialog, messagebox, ttk  # noqa: E402

from gstreco import UPDATE_URL, __version__, config, gstr1_builder, gstr3b, portal_data  # noqa: E402
from gstreco import reco, report, tally, updater  # noqa: E402
from gstreco.normalize import (fmt_date, fp, is_valid_gstin, months_between,  # noqa: E402
                               parse_period_label, period_bounds, period_label)
from gstreco.portal import PortalWorker  # noqa: E402

APP_NAME = config.APP_NAME
OWNER = config.COMPANY
MAX_SCREEN_ROWS = 3000      # larger lists are shown in part on screen; Excel export always has everything

# Only Basic-Multilingual-Plane symbols are used in labels (Tk 8.6 cannot draw emoji safely).
# ---------------------------------------------------------------- brand palette (from the logo)
C = {
    "maroon": "#900849", "maroon_dark": "#6E0537", "maroon_light": "#B0306A",
    "yellow": "#FED101", "yellow_dark": "#E5BC00", "yellow_soft": "#FFF4BF",
    "lavender": "#C09FCD", "lavender_soft": "#F1E9F6",
    "blush": "#F8EAF0", "bg": "#F6F1F4", "card": "#FFFFFF", "border": "#E7D9E0",
    "text": "#2B1A22", "muted": "#7D6872", "on_maroon_muted": "#F2C9DA",
    "green": "#2E7D32", "green_soft": "#E4F3E6", "red": "#B71C4A", "red_soft": "#FBE4EA",
    "purple": "#5E3A78", "stripe": "#FBF7F9",
}
STATUS_STYLE = {   # status -> (row background, accent / text colour)
    reco.MATCHED: (C["green_soft"], C["green"]),
    reco.PROBABLE: (C["yellow_soft"], "#8A6D00"),
    reco.MISMATCH: (C["red_soft"], C["red"]),
    reco.GSTIN_MISMATCH: (C["red_soft"], C["red"]),
    reco.BOOKS_ONLY: ("#F6D6E3", C["maroon"]),
    reco.PORTAL_ONLY: (C["lavender_soft"], C["purple"]),
}
FONT = "Segoe UI"
UI_SCALE = 1.0          # set from the screen's DPI when the window opens (1.25 / 1.5 on scaled displays)


def px(n):
    """Scale a pixel size for high-DPI displays."""
    return int(round(n * UI_SCALE))


def enable_dpi_awareness():
    """Tell Windows we draw at the real screen resolution, so it doesn't stretch (blur) the window."""
    if os.name != "nt":
        return
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)     # per-monitor-aware where available
        except Exception:  # noqa: BLE001
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:  # noqa: BLE001
        pass


def resource(*parts):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, *parts)


def period_choices(n=36):
    t = date.today()
    y, m = t.year, t.month
    out = []
    for _ in range(n):
        out.append(period_label(y, m))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out


def money(x):
    return "" if x is None else f"{x:,.2f}"


def short_money(x):
    a = abs(x)
    if a >= 1e7:
        return f"₹{x / 1e7:,.2f} Cr"
    if a >= 1e5:
        return f"₹{x / 1e5:,.2f} L"
    return f"₹{x:,.0f}"


# ================================================================== small widgets
class Kpi(tk.Frame):
    """Coloured stat tile. Optional click handler (used to filter the table)."""

    def __init__(self, parent, title, accent, soft=None, command=None, compact=False):
        super().__init__(parent, bg=C["card"], highlightthickness=1, highlightbackground=C["border"],
                         cursor="hand2" if command else "")
        self.accent, self.soft, self.active = accent, soft or C["card"], False
        self.bar = tk.Frame(self, bg=accent, width=px(5))
        self.bar.pack(side="left", fill="y")
        body = tk.Frame(self, bg=C["card"], padx=12, pady=3 if compact else 8)
        body.pack(side="left", fill="both", expand=True)
        self.value = tk.Label(body, text="–", bg=C["card"], fg=accent, font=(FONT, 13 if compact else 17, "bold"),
                              anchor="w")
        self.value.pack(fill="x")
        self.title = tk.Label(body, text=title, bg=C["card"], fg=C["text"], font=(FONT, 9, "bold"), anchor="w")
        self.title.pack(fill="x")
        self.sub = tk.Label(body, text="", bg=C["card"], fg=C["muted"], font=(FONT, 8), anchor="w")
        self.sub.pack(fill="x")
        self.body = body
        if command:
            for w in (self, body, self.value, self.title, self.sub):
                w.bind("<Button-1>", lambda e: command())
                w.bind("<Enter>", lambda e: self._hover(True))
                w.bind("<Leave>", lambda e: self._hover(False))

    def _paint(self, bg):
        for w in (self.body, self.value, self.title, self.sub):
            w.configure(bg=bg)
        self.configure(bg=bg)

    def _hover(self, on):
        if not self.active:
            self._paint(C["blush"] if on else C["card"])

    def set_active(self, on):
        self.active = on
        self._paint(self.soft if on else C["card"])
        self.configure(highlightbackground=self.accent if on else C["border"])

    def set(self, value, sub=""):
        self.value.configure(text=value)
        self.sub.configure(text=sub)


class NavItem(tk.Frame):
    def __init__(self, parent, number, text, command):
        super().__init__(parent, bg=C["card"], cursor="hand2")
        self.ind = tk.Frame(self, bg=C["card"], width=px(4))
        self.ind.pack(side="left", fill="y")
        self.num = tk.Label(self, text=number, bg=C["lavender_soft"], fg=C["maroon"], width=3,
                            font=(FONT, 9, "bold"))
        self.num.pack(side="left", padx=(12, 8), pady=9)
        self.lbl = tk.Label(self, text=text, bg=C["card"], fg=C["text"], font=(FONT, 10), anchor="w")
        self.lbl.pack(side="left", fill="x", expand=True)
        self.selected = False
        for w in (self, self.num, self.lbl):
            w.bind("<Button-1>", lambda e: command())
            w.bind("<Enter>", lambda e: self._paint(hover=True))
            w.bind("<Leave>", lambda e: self._paint())

    def _paint(self, hover=False):
        bg = C["blush"] if (self.selected or hover) else C["card"]
        self.configure(bg=bg)
        self.lbl.configure(bg=bg, fg=C["maroon"] if self.selected else C["text"],
                           font=(FONT, 10, "bold") if self.selected else (FONT, 10))
        self.ind.configure(bg=C["maroon"] if self.selected else bg)
        self.num.configure(bg=C["maroon"] if self.selected else C["lavender_soft"],
                           fg="white" if self.selected else C["maroon"])

    def select(self, on):
        self.selected = on
        self._paint()


def card(parent, padx=14, pady=12):
    outer = tk.Frame(parent, bg=C["card"], highlightthickness=1, highlightbackground=C["border"])
    inner = tk.Frame(outer, bg=C["card"], padx=padx, pady=pady)
    inner.pack(fill="both", expand=True)
    return outer, inner


def make_tree(parent, cols, widths, height=14, sort=True):
    frame = tk.Frame(parent, bg=C["card"], highlightthickness=1, highlightbackground=C["border"])
    tv = ttk.Treeview(frame, columns=cols, show="headings", height=height)
    for c_, w in zip(cols, widths):
        if sort:
            tv.heading(c_, text=c_, command=lambda c_=c_: sort_tree(tv, c_))
        else:
            tv.heading(c_, text=c_)
        num = any(k in c_.lower() for k in ("taxable", "igst", "cgst", "sgst", "cess", "tax", "total",
                                             "diff", "value", "docs", "count"))
        tv.column(c_, width=px(w), anchor="e" if num else "w", stretch=False)
    ys = ttk.Scrollbar(frame, orient="vertical", command=tv.yview)
    xs = ttk.Scrollbar(frame, orient="horizontal", command=tv.xview)
    tv.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
    tv.grid(row=0, column=0, sticky="nsew")
    ys.grid(row=0, column=1, sticky="ns")
    xs.grid(row=1, column=0, sticky="ew")
    frame.rowconfigure(0, weight=1)
    frame.columnconfigure(0, weight=1)
    for stt, (bg, _fg) in STATUS_STYLE.items():
        tv.tag_configure(stt, background=bg)
    tv.tag_configure("warn", background=C["yellow_soft"])
    tv.tag_configure("odd", background=C["stripe"])
    tv.tag_configure("bold", font=(FONT, 9, "bold"), background=C["blush"])
    return frame, tv


def sort_tree(tv, col):
    rev = getattr(tv, "_rev", False)
    items = [(tv.set(k, col), k) for k in tv.get_children("")]

    def key(v):
        try:
            return (0, float(v[0].replace(",", "")))
        except ValueError:
            return (1, v[0].lower())
    items.sort(key=key, reverse=rev)
    for i, (_, k) in enumerate(items):
        tv.move(k, "", i)
    tv._rev = not rev


# ================================================================== main window
class App(tk.Tk):
    PAGES = [("tally", "1", "Tally data"), ("portal", "2", "GST portal"), ("purchase", "3", "Purchase vs 2B"),
             ("sales", "4", "Sales & GSTR-1"), ("3b", "5", "GSTR-3B"), ("settings", "⚙", "Settings")]

    def __init__(self, selfcheck=False):
        global UI_SCALE
        enable_dpi_awareness()
        super().__init__()
        try:
            UI_SCALE = max(1.0, min(3.0, self.winfo_fpixels("1i") / 96.0))
        except tk.TclError:
            UI_SCALE = 1.0
        self.selfcheck = selfcheck
        self.title(f"{APP_NAME} {__version__}")
        self.configure(bg=C["bg"])
        self.geometry(f"{px(1360)}x{px(860)}")
        self.minsize(px(1100), px(640))
        self.compact = self.winfo_screenheight() / UI_SCALE < 900     # e.g. 1366x768 laptops, 1080p at 150%
        self.s = config.load()
        self.ui_q = queue.Queue()
        self.portal = None
        self.portal_client = None
        self.busy = None
        self.portal_jobs = [0, 0]           # done, total
        self._images = {}
        self._pb_running = False
        self._pb_det = False
        self.current_page = None
        self.client = None
        self._reset_data()

        self._styles()
        self._load_images()
        self._header()
        self._context_bar()
        self._footer()          # bottom bars are packed before the body so they always stay visible
        self._statusbar()
        self._body()
        self.after(100, self._drain)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind_all("<F5>", lambda e: self.refresh())
        self.bind_all("<Control-r>", lambda e: self.refresh())
        self.show_page("tally")
        self._fill_clients()
        if not selfcheck:
            try:
                self.state("zoomed")                          # open maximised on Windows
            except tk.TclError:
                pass
            if not self.s["clients"]:
                self.after(400, lambda: self.manage_clients(first_run=True))
            else:
                self.select_client(self.s["current_client"])
            if updater.is_installed_app() and self.update_url():
                self.after(5000, lambda: self.check_updates(silent=True))

    # ------------------------------------------------------------------ data state
    def _reset_data(self):
        self.books_out, self.books_in = [], []
        self.books_source = None            # 'tally' | 'files'
        self.tally_cache = {}               # (y, m) -> (out_docs, in_docs)
        self.masters = None
        self._file_entries = []             # every parsed portal file of this client
        self.files_2b, self.files_r1 = {}, {}   # period -> newest entry
        self.p2b, self.r1_docs, self.r1_b2cs = [], [], []
        self.purchase_rows, self.sales_rows, self.b2cs_rows, self.rows3b = [], [], [], []
        self.ran = set()                    # which analyses the user has run: 'purchase', 'sales', '3b'
        self.gstr1_files = []

    # ------------------------------------------------------------------ style
    def _styles(self):
        st = ttk.Style(self)
        st.theme_use("clam")
        self.option_add("*Font", (FONT, 10))
        self.option_add("*TCombobox*Listbox.font", (FONT, 10))
        self.option_add("*TCombobox*Listbox.selectBackground", C["lavender"])
        self.option_add("*TCombobox*Listbox.selectForeground", C["text"])
        st.configure(".", background=C["bg"], foreground=C["text"], font=(FONT, 10), bordercolor=C["border"],
                     focuscolor=C["lavender"])
        st.configure("TFrame", background=C["bg"])
        st.configure("TLabel", background=C["bg"], foreground=C["text"])

        def btn(name, bg, fg, hover, press, border=None, bold=False, pad=(14, 7)):
            st.configure(name, background=bg, foreground=fg, bordercolor=border or bg, lightcolor=bg,
                         darkcolor=bg, focuscolor=bg, padding=(px(pad[0]), px(pad[1])), relief="flat",
                         font=(FONT, 10, "bold") if bold else (FONT, 10))
            st.map(name, background=[("disabled", "#E9E1E5"), ("pressed", press), ("active", hover)],
                   foreground=[("disabled", "#A99AA2")],
                   lightcolor=[("pressed", press), ("active", hover)],
                   darkcolor=[("pressed", press), ("active", hover)],
                   bordercolor=[("active", border or hover)])
        btn("TButton", C["card"], C["text"], C["blush"], C["lavender_soft"], border=C["border"])
        btn("Primary.TButton", C["maroon"], "white", C["maroon_light"], C["maroon_dark"], bold=True)
        btn("Accent.TButton", C["yellow"], C["text"], C["yellow_dark"], "#CCA700", bold=True)
        btn("Header.TButton", C["maroon_dark"], "white", C["maroon_light"], "#560329", pad=(12, 6))
        btn("Link.TButton", C["card"], C["maroon"], C["blush"], C["lavender_soft"], pad=(6, 3))

        st.configure("TCombobox", fieldbackground=C["card"], background=C["card"], foreground=C["text"],
                     arrowcolor=C["maroon"], bordercolor=C["border"], lightcolor=C["card"], darkcolor=C["card"],
                     padding=(px(6), px(4)), selectbackground=C["card"], selectforeground=C["text"])
        st.map("TCombobox", fieldbackground=[("readonly", C["card"])], selectbackground=[("readonly", C["card"])],
               selectforeground=[("readonly", C["text"])], bordercolor=[("focus", C["maroon"])])
        st.configure("TEntry", fieldbackground=C["card"], bordercolor=C["border"], lightcolor=C["card"],
                     darkcolor=C["card"], padding=(px(6), px(4)))
        st.map("TEntry", bordercolor=[("focus", C["maroon"])])
        st.configure("Brand.Horizontal.TProgressbar", troughcolor=C["blush"], background=C["maroon"],
                     bordercolor=C["blush"], lightcolor=C["maroon_light"], darkcolor=C["maroon"])
        st.configure("Treeview", background=C["card"], fieldbackground=C["card"], foreground=C["text"],
                     rowheight=px(26), bordercolor=C["border"], lightcolor=C["card"], darkcolor=C["card"],
                     font=(FONT, 9))
        st.map("Treeview", background=[("selected", C["lavender"])], foreground=[("selected", C["text"])])
        st.configure("Treeview.Heading", background=C["maroon"], foreground="white", relief="flat",
                     font=(FONT, 9, "bold"), padding=(px(6), px(6)), bordercolor=C["maroon_dark"],
                     lightcolor=C["maroon"], darkcolor=C["maroon"])
        st.map("Treeview.Heading", background=[("active", C["maroon_light"])])
        for o in ("Vertical", "Horizontal"):
            st.configure(f"{o}.TScrollbar", background=C["border"], troughcolor=C["bg"], bordercolor=C["bg"],
                         arrowcolor=C["maroon"], lightcolor=C["border"], darkcolor=C["border"])

    def _load_images(self):
        steps = [100, 125, 150, 175, 200, 250, 300]
        pct = min(steps, key=lambda v: abs(v - UI_SCALE * 100))      # sharp image made for this scaling
        for key, name in (("icon", "app.png"), ("icon32", "icon32.png"), ("mark", f"header_mark@{pct}.png"),
                          ("logo", f"logo@{pct}.png")):
            try:
                self._images[key] = tk.PhotoImage(file=resource("assets", name))
            except tk.TclError:
                self._images[key] = None
        icons = [i for i in (self._images.get("icon"), self._images.get("icon32")) if i]
        if icons:
            self.iconphoto(True, *icons)

    # ------------------------------------------------------------------ header
    def _header(self):
        h = tk.Frame(self, bg=C["maroon"], height=px(64 if self.compact else 72))
        h.pack(fill="x")
        h.pack_propagate(False)
        tk.Frame(self, bg=C["yellow"], height=px(3)).pack(fill="x")
        left = tk.Frame(h, bg=C["maroon"])
        left.pack(side="left", padx=(16, 0))
        if self._images.get("mark"):
            tk.Label(left, image=self._images["mark"], bg=C["maroon"]).pack(side="left", pady=7)
        words = tk.Frame(left, bg=C["maroon"])
        words.pack(side="left", padx=12)
        line = tk.Frame(words, bg=C["maroon"])
        line.pack(anchor="w")
        tk.Label(line, text="GST Recon", bg=C["maroon"], fg="white", font=(FONT, 20, "bold italic")).pack(side="left")
        tk.Label(line, text="X", bg=C["maroon"], fg=C["yellow"], font=(FONT, 20, "bold italic")).pack(side="left")
        tk.Label(words, text="Tally ↔ GST portal reconciliation", bg=C["maroon"], fg=C["on_maroon_muted"],
                 font=(FONT, 9)).pack(anchor="w")

        right = tk.Frame(h, bg=C["maroon"])
        right.pack(side="right", padx=16)
        upd = tk.Frame(right, bg=C["maroon"])
        upd.pack(side="right", padx=(18, 0))
        self.btn_update = ttk.Button(upd, text="⟳  Check for updates", style="Accent.TButton",
                                     command=self.check_updates)
        self.btn_update.pack()
        tk.Label(upd, text=f"Version {__version__}", bg=C["maroon"], fg=C["on_maroon_muted"],
                 font=(FONT, 8)).pack(pady=(2, 0))
        cl = tk.Frame(right, bg=C["maroon"])
        cl.pack(side="right")
        tk.Label(cl, text="CLIENT", bg=C["maroon"], fg=C["on_maroon_muted"], font=(FONT, 8, "bold")).grid(
            row=0, column=0, sticky="w")
        self.v_client = tk.StringVar()
        self.cb_client = ttk.Combobox(cl, textvariable=self.v_client, state="readonly", width=38,
                                      font=(FONT, 10, "bold"))
        self.cb_client.grid(row=1, column=0)
        self.cb_client.bind("<<ComboboxSelected>>", self._client_chosen)
        ttk.Button(cl, text="Manage clients", style="Header.TButton", command=self.manage_clients).grid(
            row=1, column=1, padx=(8, 0))

    # ------------------------------------------------------------------ context bar (period + refresh)
    def _context_bar(self):
        bar = tk.Frame(self, bg=C["card"])
        bar.pack(fill="x")
        tk.Frame(self, bg=C["border"], height=1).pack(fill="x")
        inner = tk.Frame(bar, bg=C["card"], padx=16, pady=5 if self.compact else 10)
        inner.pack(fill="x")

        def field(col, label, make):
            f = tk.Frame(inner, bg=C["card"])
            f.grid(row=0, column=col, padx=(0, 18), sticky="w")
            tk.Label(f, text=label, bg=C["card"], fg=C["muted"], font=(FONT, 8, "bold")).pack(anchor="w")
            w = make(f)
            w.pack(anchor="w")
            return w

        self.lbl_gstin = field(0, "GSTIN", lambda f: tk.Label(f, text="—", bg=C["card"], fg=C["text"],
                                                              font=("Consolas", 11, "bold")))
        self.lbl_company = field(1, "TALLY COMPANY", lambda f: tk.Label(
            f, text="—", bg=C["card"], fg=C["text"], font=(FONT, 10), width=30, anchor="w"))
        choices = period_choices()
        default = choices[1] if len(choices) > 1 else choices[0]
        self.v_from = tk.StringVar(value=default)
        self.v_to = tk.StringVar(value=default)
        cb_from = field(2, "FROM", lambda f: ttk.Combobox(f, textvariable=self.v_from, values=choices, width=10,
                                                          state="readonly"))
        cb_to = field(3, "TO", lambda f: ttk.Combobox(f, textvariable=self.v_to, values=choices, width=10,
                                                      state="readonly"))
        for cb in (cb_from, cb_to):
            cb.bind("<<ComboboxSelected>>", lambda e: self._period_changed())
        self.btn_refresh = ttk.Button(inner, text="⟳  Refresh", style="Primary.TButton", command=self.refresh)
        self.btn_refresh.grid(row=0, column=4, sticky="s", padx=(0, 12))
        self.lbl_stale = tk.Label(inner, text="", bg=C["card"], fg="#8A6D00", font=(FONT, 9, "bold"))
        self.lbl_stale.grid(row=0, column=5, sticky="sw", pady=(0, 6))
        inner.columnconfigure(6, weight=1)
        chips = tk.Frame(inner, bg=C["card"])
        chips.grid(row=0, column=7, sticky="e")
        self.chips = {}
        for key, label in (("books", "Books"), ("2b", "GSTR-2B"), ("r1", "GSTR-1 filed")):
            f = tk.Frame(chips, bg=C["lavender_soft"], padx=10, pady=4)
            f.pack(side="left", padx=4)
            tk.Label(f, text=label.upper(), bg=C["lavender_soft"], fg=C["purple"], font=(FONT, 7, "bold")).pack(
                anchor="w")
            v = tk.Label(f, text="—", bg=C["lavender_soft"], fg=C["text"], font=(FONT, 10, "bold"))
            v.pack(anchor="w")
            self.chips[key] = v

    # ------------------------------------------------------------------ body: sidebar + pages
    def _body(self):
        body = tk.Frame(self, bg=C["bg"])
        body.pack(fill="both", expand=True)
        side = tk.Frame(body, bg=C["card"], width=px(214))
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        tk.Frame(body, bg=C["border"], width=1).pack(side="left", fill="y")
        tk.Label(side, text="WORKFLOW", bg=C["card"], fg=C["muted"], font=(FONT, 8, "bold")).pack(
            anchor="w", padx=18, pady=(16, 6))
        self.nav = {}
        for key, num, text in self.PAGES:
            if key == "settings":
                tk.Frame(side, bg=C["border"], height=1).pack(fill="x", padx=14, pady=10)
            item = NavItem(side, num, text, lambda k=key: self.show_page(k))
            item.pack(fill="x")
            self.nav[key] = item
        foot = tk.Frame(side, bg=C["card"])
        foot.pack(side="bottom", fill="x", padx=14, pady=14)
        ttk.Button(foot, text="Open client folder", style="Link.TButton",
                   command=self.open_client_folder).pack(anchor="w")
        ttk.Button(foot, text="ℹ  About", style="Link.TButton", command=self.about).pack(anchor="w")

        self.content = tk.Frame(body, bg=C["bg"])
        self.content.pack(side="left", fill="both", expand=True)
        self.content.rowconfigure(0, weight=1)
        self.content.columnconfigure(0, weight=1)
        self.pages = {}
        for key, _, _ in self.PAGES:
            f = tk.Frame(self.content, bg=C["bg"], padx=20, pady=8 if self.compact else 16)
            f.grid(row=0, column=0, sticky="nsew")
            self.pages[key] = f
        self._page_tally(self.pages["tally"])
        self._page_portal(self.pages["portal"])
        self._page_purchase(self.pages["purchase"])
        self._page_sales(self.pages["sales"])
        self._page_3b(self.pages["3b"])
        self._page_settings(self.pages["settings"])

    def show_page(self, key):
        self.pages[key].tkraise()
        for k, item in self.nav.items():
            item.select(k == key)
        self.current_page = key

    def _page_head(self, page, title, subtitle):
        head = tk.Frame(page, bg=C["bg"])
        head.pack(fill="x", pady=(0, 6 if self.compact else 12))
        tk.Label(head, text=title, bg=C["bg"], fg=C["maroon"], font=(FONT, 14 if self.compact else 17, "bold")).pack(
            anchor="w")
        tk.Label(head, text=subtitle, bg=C["bg"], fg=C["muted"], font=(FONT, 9)).pack(anchor="w")
        return head

    def _toolbar(self, page):
        outer, inner = card(page, padx=12, pady=6 if self.compact else 10)
        outer.pack(fill="x", pady=(0, 8 if self.compact else 12))
        return inner

    # ------------------------------------------------------------------ status bar, activity, footer
    def _footer(self):
        f = tk.Frame(self, bg=C["maroon"])
        f.pack(side="bottom", fill="x")
        tk.Label(f, text=f"© {date.today().year} {OWNER}  ·  {APP_NAME} is developed and owned by {OWNER}. "
                         f"All rights reserved.",
                 bg=C["maroon"], fg=C["on_maroon_muted"], font=(FONT, 8), pady=4).pack(side="left", padx=16)
        tk.Label(f, text="Review all figures before filing. Login and filing on the GST portal are done by you.",
                 bg=C["maroon"], fg=C["on_maroon_muted"], font=(FONT, 8), pady=4).pack(side="right", padx=16)

    def _statusbar(self):
        sb = tk.Frame(self, bg=C["card"], padx=16, pady=6)
        sb.pack(side="bottom", fill="x")
        tk.Frame(self, bg=C["border"], height=1).pack(side="bottom", fill="x")
        self.pb = ttk.Progressbar(sb, style="Brand.Horizontal.TProgressbar", length=px(280), mode="determinate",
                                  maximum=100)
        self.pb.pack(side="left")
        self.lbl_pct = tk.Label(sb, text="", bg=C["card"], fg=C["maroon"], font=(FONT, 9, "bold"), width=5)
        self.lbl_pct.pack(side="left")
        self.lbl_status = tk.Label(sb, text="Ready", bg=C["card"], fg=C["text"], font=(FONT, 9), anchor="w")
        self.lbl_status.pack(side="left", fill="x", expand=True, padx=(6, 0))
        self.btn_log = ttk.Button(sb, text="Activity log ▾", style="Link.TButton", command=self._toggle_log)
        self.btn_log.pack(side="right")
        self.activity = tk.Frame(self, bg=C["card"])
        self.logtxt = tk.Text(self.activity, height=6, state="disabled", wrap="word", font=("Consolas", 9),
                              bg="#FCFAFB", fg=C["text"], relief="flat", padx=12, pady=6, highlightthickness=0)
        self.logtxt.pack(fill="both", expand=True)
        self._log_border = tk.Frame(self, bg=C["border"], height=1)
        self._log_border.pack(side="bottom", fill="x")
        self._log_visible = False               # hidden by default: the status bar shows the latest message
        self.btn_log.configure(text="Activity log ▴")

    def _toggle_log(self):
        if self._log_visible:
            self.activity.pack_forget()
            self.btn_log.configure(text="Activity log ▴")
        else:
            self.activity.pack(side="bottom", fill="x", before=self._log_border)
            self.btn_log.configure(text="Activity log ▾")
        self._log_visible = not self._log_visible

    # ------------------------------------------------------------------ threading / progress plumbing
    def log(self, msg):
        self.ui_q.put(("log", msg))

    def progress(self, msg=None, frac=None):
        """Thread-safe. frac None -> busy animation (or text-only during a measured job); 0..1 -> percentage."""
        self.ui_q.put(("progress", (msg, frac)))

    def on_ui(self, fn):
        self.ui_q.put(("call", fn))

    def _drain(self):
        try:
            for _ in range(500):
                kind, payload = self.ui_q.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "progress":
                    self._apply_progress(*payload)
                elif kind == "call":
                    payload()
        except queue.Empty:
            pass
        except Exception:  # noqa: BLE001
            self.report_callback_exception(*sys.exc_info())
        self.after(80, self._drain)

    def _append_log(self, text):
        self.logtxt.configure(state="normal")
        self.logtxt.insert("end", f"{time.strftime('%H:%M:%S')}  {text}\n")
        lines = int(self.logtxt.index("end-1c").split(".")[0])
        if lines > 2000:
            self.logtxt.delete("1.0", f"{lines - 2000}.0")
        self.logtxt.see("end")
        self.logtxt.configure(state="disabled")

    def _apply_progress(self, msg, frac):
        if msg:
            self.lbl_status.configure(text=msg.strip())
            self._append_log(msg)
        if frac is None:
            if self._pb_det:
                return                      # keep showing the percentage of the current job
            if not self._pb_running:
                self.pb.configure(mode="indeterminate")
                self.pb.start(12)
                self._pb_running = True
            self.lbl_pct.configure(text="")
        else:
            self._pb_det = True
            if self._pb_running:
                self.pb.stop()
                self._pb_running = False
            self.pb.configure(mode="determinate", value=max(0, min(100, frac * 100)))
            self.lbl_pct.configure(text=f"{frac * 100:.0f}%")

    def _progress_start(self):
        self._pb_det = False

    def _progress_done(self, msg="Ready"):
        if self._pb_running:
            self.pb.stop()
            self._pb_running = False
        self._pb_det = False
        self.pb.configure(mode="determinate", value=100)
        self.lbl_pct.configure(text="✓")
        self.lbl_status.configure(text=msg)

        def clear():
            if not self.busy and not self._pb_running and not self._pb_det:
                self.pb.configure(value=0)
                self.lbl_pct.configure(text="")
        self.after(2500, clear)

    def run_bg(self, label, fn, done=None, fail=None):
        """Runs fn(progress) on a worker thread with the progress bar; done(result) on the UI thread."""
        if self.busy:
            self.lbl_status.configure(text=f"Please wait – {self.busy} is still running…")
            return False
        self.busy = label
        self.btn_refresh.state(["disabled"])
        self.config(cursor="watch")
        self._progress_start()
        self._apply_progress(f"{label}…", None)

        def finish():
            self.busy = None
            self.btn_refresh.state(["!disabled"])
            self.config(cursor="")

        def worker():
            try:
                res = fn(self.progress)
            except Exception as e:  # noqa: BLE001
                tb = traceback.format_exc()

                def err():
                    finish()
                    self._progress_done(f"{label} failed")
                    self._append_log(tb)
                    if fail:
                        fail(e)
                    else:
                        self.error(str(e))
                self.on_ui(err)
                return

            def ok():
                finish()
                self._progress_done(f"{label} – done")
                if done:
                    done(res)
            self.on_ui(ok)
        threading.Thread(target=worker, daemon=True).start()
        return True

    def info(self, msg, title=APP_NAME):
        if self.selfcheck:
            print("INFO:", msg)
            return
        messagebox.showinfo(title, msg, parent=self)

    def error(self, msg, title=APP_NAME):
        if self.selfcheck:
            raise RuntimeError(msg)
        messagebox.showerror(title, msg, parent=self)

    def ask(self, msg, title=APP_NAME):
        if self.selfcheck:
            return True
        return messagebox.askyesno(title, msg, parent=self)

    def _on_close(self):
        self._save_settings(quiet=True)
        if self.portal:
            self.portal.stop()
        self.destroy()

    # ================================================================== clients
    def _fill_clients(self):
        names = [self._client_label(c) for c in self.s["clients"]]
        self.cb_client["values"] = names
        if self.client:
            self.v_client.set(self._client_label(self.client))
        elif not names:
            self.v_client.set("Add a client to start  →")

    @staticmethod
    def _client_label(c):
        return f"{c['name']}  ·  {c.get('gstin') or 'no GSTIN'}"

    def _client_chosen(self, _e=None):
        i = self.cb_client.current()
        if 0 <= i < len(self.s["clients"]):
            cid = self.s["clients"][i]["id"]
            if not self.client or cid != self.client["id"]:
                self.select_client(cid)

    def select_client(self, cid):
        c = next((x for x in self.s["clients"] if x["id"] == cid), None)
        if not c:
            return
        if self.busy:
            self.lbl_status.configure(text=f"Please wait – {self.busy} is still running…")
            self._fill_clients()
            return
        if self.portal and self.portal_client != cid:
            self.portal.stop()
            self.portal = None
            self.lbl_portal.configure(text="Browser not started.")
        self.client = c
        self.s["current_client"] = cid
        self._save_settings(quiet=True)
        self._reset_data()
        self._show_client_labels()
        self._fill_clients()
        self._clear_views()
        self._update_state()
        self.log(f"Client selected: {c['name']} ({c.get('gstin') or 'no GSTIN'})")
        self.load_saved_portal_files()

    def _show_client_labels(self):
        c = self.client or {}
        self.lbl_gstin.configure(text=c.get("gstin") or "—")
        self.lbl_company.configure(text=c.get("tally_company") or "(set in Manage clients)")

    def need_client(self):
        if not self.client:
            self.info("Add or choose a client first (top right: Manage clients).")
            return False
        if not is_valid_gstin(self.client.get("gstin")):
            self.info("This client has no valid GSTIN. Open Manage clients and enter it.")
            return False
        return True

    def gstin(self):
        return (self.client or {}).get("gstin", "")

    def client_dir(self):
        return config.client_dir(self.s, self.client)

    def out_dir(self):
        a, b = self.periods()
        tag = period_label(*a) + ("" if a == b else "_to_" + period_label(*b))
        d = os.path.join(self.client_dir(), tag)
        os.makedirs(d, exist_ok=True)
        return d

    def open_client_folder(self):
        if not self.client:
            return self.info("Choose a client first.")
        self._opened(self.client_dir())

    def manage_clients(self, first_run=False):
        return ClientsDialog(self, first_run)

    def clients_changed(self, select_id=None):
        self._save_settings(quiet=True)
        if select_id:
            self.select_client(select_id)
        elif self.client and not any(c["id"] == self.client["id"] for c in self.s["clients"]):
            self.client = None
            self._reset_data()
            self._clear_views()
            self._show_client_labels()
            if self.s["clients"]:
                self.select_client(self.s["clients"][0]["id"])
        elif self.client:
            self.client = next(c for c in self.s["clients"] if c["id"] == self.client["id"])
            self._show_client_labels()
        self._fill_clients()
        self._update_state()

    # ================================================================== period + refresh
    def periods(self):
        a, b = parse_period_label(self.v_from.get()), parse_period_label(self.v_to.get())
        if a > b:
            a, b = b, a
        return a, b

    def months(self):
        return months_between(*self.periods())

    def _period_changed(self):
        if not self.client:
            return
        self._apply_portal_selection()
        months = self.months()
        if self.books_source == "tally":
            missing = [m for m in months if m not in self.tally_cache]
            if not missing:
                self._apply_tally_cache()
                self.lbl_status.configure(text=f"Showing {period_label(*months[0])} – {period_label(*months[-1])} "
                                               f"(already loaded, no need to refresh).")
            else:
                self._fetch_tally(missing)
        elif self.books_source == "files":
            self.lbl_stale.configure(text="⚠ Dates changed – load the Tally XML files for these months")

    def refresh(self):
        """Re-read everything for the selected client and months (Tally + saved portal files)."""
        if not self.need_client():
            return
        if self.busy:
            self.lbl_status.configure(text=f"Please wait – {self.busy} is still running…")
            return
        if self.client.get("tally_company"):
            self.tally_cache, self.masters = {}, None
            self._fetch_tally(self.months(), then=self.load_saved_portal_files)
        else:
            self.load_saved_portal_files()
            self.info("Set the Tally company for this client in Manage clients to load books directly from "
                      "Tally (or use 'Load Tally XML files' on page 1).")

    def _fetch_tally(self, months, then=None):
        if not self.need_client():
            return
        company = self.client.get("tally_company", "")
        if not company:
            self.info("Set the Tally company name for this client in Manage clients (you can list the "
                      "companies open in Tally there).")
            return
        c = tally.TallyClient(self.s["tally_host"], self.s["tally_port"])
        g, masters = self.gstin(), self.masters
        cache = os.path.join(self.out_dir(), "tally_raw")
        cid = self.client["id"]

        def work(progress):
            return tally.fetch_months(c, company, g, months, masters, cache, progress)

        def done(res):
            if not self.client or self.client["id"] != cid:
                return
            self.masters, got = res
            self.tally_cache.update(got)
            self.books_source = "tally"
            self._apply_tally_cache()
            if then:
                then()
        label = (f"Loading {period_label(*months[0])}" + ("" if len(months) == 1 else
                                                         f" – {period_label(*months[-1])}") + " from Tally")
        self.run_bg(label, work, done)

    def _apply_tally_cache(self):
        out_docs, in_docs = [], []
        for ym in self.months():
            o, i = self.tally_cache.get(ym, ([], []))
            out_docs += o
            in_docs += i
        self.lbl_stale.configure(text="")
        self._set_books((out_docs, in_docs))

    def _rerun_done(self):
        """After data changes, re-run whatever the user already ran (each takes about a second)."""
        if "purchase" in self.ran:
            self.run_purchase(quiet=True)
        if "sales" in self.ran:
            self.run_sales(quiet=True)
        if "3b" in self.ran:
            self.run_3b(quiet=True)

    def _update_state(self):
        self.chips["books"].configure(text=f"{len(self.books_out):,} S · {len(self.books_in):,} P")
        self.chips["2b"].configure(text=f"{len(self.p2b):,} docs")
        self.chips["r1"].configure(text=f"{len(self.r1_docs) + len(self.r1_b2cs):,} rows")

    def _clear_views(self):
        for tv in (self.tv_books, self.tv_p, self.tv_s, self.tv_3b, self.tv_r1sum, self.tv_files):
            tv.delete(*tv.get_children())
        for k in list(self.kpi_books.values()) + list(self.kpi_p.values()) + list(self.kpi_s.values()):
            k.set("–", "")
            k.set_active(False)
        self.txt_r1warn.delete("1.0", "end")
        self.lbl_stale.configure(text="")
        self.lbl_itc.configure(text="")

    # ================================================================== page 1: Tally data
    def _page_tally(self, page):
        self._page_head(page, "Tally data", "Sales, purchases, credit and debit notes for the selected client and "
                                            "months. Rows with problems are highlighted and listed first.")
        tb = self._toolbar(page)
        ttk.Button(tb, text="⟳  Fetch from Tally", style="Primary.TButton", command=self.refresh).pack(side="left")
        ttk.Button(tb, text="Load Tally XML files…", command=self.load_tally_files).pack(side="left", padx=8)
        ttk.Button(tb, text="⬇  Export to Excel", command=self.export_books).pack(side="left")
        ttk.Button(tb, text="✔  Test Tally connection", command=self.test_tally).pack(side="left", padx=8)
        tk.Label(tb, text="Show", bg=C["card"], fg=C["muted"]).pack(side="left", padx=(12, 6))
        self.v_bfilter = tk.StringVar(value="All")
        cb = ttk.Combobox(tb, textvariable=self.v_bfilter, state="readonly", width=16,
                          values=["All", "Sales", "Purchases", "With warnings"])
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", lambda e: self._fill_books())

        row = tk.Frame(page, bg=C["bg"])
        row.pack(fill="x", pady=(0, 12))
        self.kpi_books = {}
        for i, (k, t, col) in enumerate([("sales", "Sales & credit notes", C["maroon"]),
                                         ("purch", "Purchases & debit notes", C["purple"]),
                                         ("warn", "Rows with warnings", "#8A6D00"),
                                         ("out", "Outward taxable value", C["green"])]):
            w = Kpi(row, t, col, compact=self.compact)
            w.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 10, 0))
            row.columnconfigure(i, weight=1)
            self.kpi_books[k] = w
        cols = ["Dir", "Type", "Date", "Voucher", "Inv no", "Party", "GSTIN", "POS", "Taxable", "IGST",
                "CGST", "SGST", "Cess", "Total", "Rates", "Warnings"]
        f, self.tv_books = make_tree(page, cols, [50, 45, 85, 90, 120, 210, 140, 40, 100, 85, 85, 85, 60, 100,
                                                  70, 420])
        f.pack(fill="both", expand=True)

    def test_tally(self):
        if not self.client:
            return self.info("Choose a client first.")
        c = tally.TallyClient(self.s["tally_host"], self.s["tally_port"], timeout=60)
        company, g = self.client.get("tally_company", ""), self.gstin()
        day = date(*self.periods()[0], 1)
        self.run_bg("Testing Tally connection", lambda p: tally.diagnose(c, company, g, day, p),
                    lambda res: CheckDialog(self, "Tally connection test", res))

    def load_tally_files(self):
        if not self.need_client():
            return
        paths = filedialog.askopenfilenames(title="Tally XML (masters + vouchers / Day Book)",
                                            filetypes=[("XML", "*.xml"), ("All", "*.*")])
        if not paths:
            return
        g = self.gstin()
        d_from, d_to = period_bounds(*self.periods())

        def done(res):
            self.books_source = "files"
            self.tally_cache = {}
            self.lbl_stale.configure(text="")
            self._set_books(res)
        self.run_bg("Reading Tally XML", lambda p: tally.load_from_files(paths, g, d_from, d_to, p), done)

    def _set_books(self, res):
        self.books_out, self.books_in = res
        self._fill_books()
        docs = self.books_out + self.books_in
        nw = sum(1 for d in docs if d.warnings)
        out_tax = sum(d.sign * d.taxable for d in self.books_out)
        self.kpi_books["sales"].set(f"{len(self.books_out):,}",
                                    f"{sum(d.doc_type == 'CN' for d in self.books_out)} credit notes")
        self.kpi_books["purch"].set(f"{len(self.books_in):,}",
                                    f"{sum(d.doc_type == 'DN' for d in self.books_in)} debit notes")
        self.kpi_books["warn"].set(f"{nw:,}", "check before filing" if nw else "all clear")
        self.kpi_books["out"].set(short_money(out_tax),
                                  f"tax {short_money(sum(d.sign * d.tax for d in self.books_out))}")
        self.log(f"Books: {len(self.books_out):,} sales/credit notes, {len(self.books_in):,} purchases/debit notes, "
                 f"{nw:,} with warnings.")
        self._update_state()
        self._rerun_done()

    def _fill_books(self):
        tv = self.tv_books
        tv.delete(*tv.get_children())
        flt = self.v_bfilter.get()
        docs = self.books_out if flt == "Sales" else self.books_in if flt == "Purchases" else \
            self.books_out + self.books_in
        if flt == "With warnings":
            docs = [d for d in docs if d.warnings]
        docs = [d for d in docs if d.warnings] + [d for d in docs if not d.warnings]
        for n, d in enumerate(docs[:MAX_SCREEN_ROWS]):
            tag = ("warn",) if d.warnings else (("odd",) if n % 2 else ())
            tv.insert("", "end", tags=tag, values=[
                "Sale" if d.direction == "out" else "Purch", d.doc_type, fmt_date(d.inv_date), d.voucher_no,
                d.inv_no, d.party, d.gstin, d.pos, money(d.taxable), money(d.igst), money(d.cgst),
                money(d.sgst), money(d.cess), money(d.total), ", ".join(f"{r.rate:g}%" for r in d.rate_lines),
                "; ".join(d.warnings)])
        if len(docs) > MAX_SCREEN_ROWS:
            self.log(f"Showing the first {MAX_SCREEN_ROWS:,} of {len(docs):,} rows on screen – all rows are used "
                     f"for reconciliation and in Export to Excel.")

    def export_books(self):
        if not (self.books_out or self.books_in):
            return self.info("Load Tally data first.")
        p = os.path.join(self.out_dir(), "Books_GST_register.xlsx")
        docs = self.books_out + self.books_in
        self.run_bg("Writing Excel", lambda pr: report.write_books(p, docs, "Books"), self._opened)

    # ================================================================== page 2: GST portal
    def _page_portal(self, page):
        self._page_head(page, "GST portal", "Download GSTR-2B and filed GSTR-1, or upload your GSTR-1 file. You log "
                                            "in yourself – the app never sees your password or OTP.")
        grid = tk.Frame(page, bg=C["bg"])
        grid.pack(fill="x")
        grid.columnconfigure(0, weight=3)
        grid.columnconfigure(1, weight=2)
        o1, c1 = card(grid, 16, 14)
        o1.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        tk.Label(c1, text="Assisted portal", bg=C["card"], fg=C["maroon"], font=(FONT, 12, "bold")).pack(anchor="w")
        tk.Label(c1, text="Opens Edge/Chrome with a separate saved profile for this client.", bg=C["card"],
                 fg=C["muted"], font=(FONT, 9)).pack(anchor="w", pady=(0, 10))
        b = tk.Frame(c1, bg=C["card"])
        b.pack(fill="x")
        for i, (txt, cmd, sty) in enumerate([
                ("Open portal & log in", self.portal_login, "Primary.TButton"),
                ("⬇  Download GSTR-2B (From–To)", self.portal_2b, "TButton"),
                ("⬇  Download filed GSTR-1", self.portal_r1_download, "TButton"),
                ("⬆  Upload GSTR-1 JSON…", self.portal_r1_upload, "TButton")]):
            ttk.Button(b, text=txt, style=sty, command=cmd).grid(row=i // 2, column=i % 2, sticky="ew", padx=4,
                                                                 pady=4)
        b.columnconfigure(0, weight=1)
        b.columnconfigure(1, weight=1)
        self.lbl_portal = tk.Label(c1, text="Browser not started.", bg=C["card"], fg=C["muted"], font=(FONT, 9))
        self.lbl_portal.pack(anchor="w", pady=(10, 0))

        o2, c2 = card(grid, 16, 14)
        o2.grid(row=0, column=1, sticky="nsew")
        tk.Label(c2, text="Files you already have", bg=C["card"], fg=C["maroon"], font=(FONT, 12, "bold")).pack(
            anchor="w")
        tk.Label(c2, text="They are copied into this client's folder and remembered next time.", bg=C["card"],
                 fg=C["muted"], font=(FONT, 9), wraplength=px(380), justify="left").pack(anchor="w", pady=(0, 10))
        ttk.Button(c2, text="Add GSTR-2B / GSTR-1 files…", command=self.add_portal_files).pack(anchor="w")
        ttk.Button(c2, text="Open portal files folder", style="Link.TButton",
                   command=lambda: self.client and self._opened(config.portal_files_dir(self.s, self.client))
                   ).pack(anchor="w", pady=(6, 0))

        tk.Label(page, text="Saved portal files for this client", bg=C["bg"], fg=C["text"],
                 font=(FONT, 10, "bold")).pack(anchor="w", pady=(16, 6))
        f, self.tv_files = make_tree(page, ["Return", "Period", "Docs", "Status", "File"],
                                     [90, 90, 80, 260, 520], height=8)
        f.pack(fill="both", expand=True)
        tip = ("If the portal layout changes and an automatic click fails, the status bar tells you which button to "
               "click yourself – the downloaded file is still captured. Uploading GSTR-1 only fills the draft; "
               "review it on the portal and file with DSC/EVC yourself.")
        tk.Label(page, text=tip, bg=C["bg"], fg=C["muted"], font=(FONT, 8), wraplength=px(1000), justify="left").pack(
            anchor="w", pady=(8, 0))

    # ---------- portal files (parse on a worker thread, apply on the UI thread)
    def load_saved_portal_files(self):
        if not self.client:
            return
        folder = config.portal_files_dir(self.s, self.client)
        paths = [os.path.join(folder, n) for n in sorted(os.listdir(folder))
                 if n.lower().endswith((".json", ".zip"))]
        if not paths or self.selfcheck:
            self._apply_portal_files(self._parse_portal_files(paths))
            return
        self.run_bg("Loading saved portal files", lambda p: self._parse_portal_files(paths, p),
                    self._apply_portal_files)

    @staticmethod
    def _parse_portal_files(paths, progress=None):
        out = []
        for n, path in enumerate(paths):
            if progress:
                progress(f"Reading {os.path.basename(path)}", n / max(1, len(paths)))
            try:
                found = portal_data.read_portal_file(path)
            except Exception as ex:  # noqa: BLE001
                found = [{"kind": "?", "error": str(ex), "period": "", "docs": [], "b2cs": [], "gstin": ""}]
            if not found:
                found = [{"kind": "?", "error": "not a GSTR-2B/GSTR-1 file", "period": "", "docs": [], "b2cs": [],
                          "gstin": ""}]
            for e in found:
                e["path"], e["mtime"] = path, os.path.getmtime(path)
                out.append(e)
        return out

    def _apply_portal_files(self, entries, merge=False):
        if not merge:
            self._file_entries = []
        known = {(e["path"], e["kind"], e["period"]) for e in self._file_entries}
        self._file_entries += [e for e in entries if (e["path"], e["kind"], e["period"]) not in known]
        mine = self.gstin()
        self.files_2b, self.files_r1 = {}, {}
        status = {}
        for e in sorted(self._file_entries, key=lambda e: e["mtime"]):      # newest file per period wins
            k = id(e)
            if e["kind"] == "?":
                status[k] = f"Not used ({e.get('error', '')[:70]})"
                continue
            if mine and e["gstin"] and e["gstin"] != mine:
                status[k] = f"Skipped – belongs to GSTIN {e['gstin']}"
                continue
            bucket = self.files_2b if e["kind"] == "2B" else self.files_r1
            key = e["period"] or f"?{e['path']}"
            if key in bucket:
                status[id(bucket[key])] = "Replaced by a newer file"
            bucket[key] = e
            status[k] = "✔ In use"
        tv = self.tv_files
        tv.delete(*tv.get_children())
        rows = sorted(self._file_entries, key=lambda e: (e["kind"], e["period"][2:] + e["period"][:2]), reverse=True)
        for n, e in enumerate(rows):
            tv.insert("", "end", values=[e["kind"], self._fmt_period(e["period"]),
                                         f"{len(e['docs']) + len(e['b2cs']):,}", status.get(id(e), ""),
                                         os.path.basename(e["path"])], tags=("odd",) if n % 2 else ())
        self._apply_portal_selection()

    @staticmethod
    def _fmt_period(p):
        if p and len(p) == 6 and p.isdigit():
            return period_label(int(p[2:]), int(p[:2]))
        return p or "?"

    def _apply_portal_selection(self):
        wanted = {fp(y, m) for (y, m) in self.months()}
        self.p2b = [d for k, e in self.files_2b.items() if k in wanted or k.startswith("?") for d in e["docs"]]
        r1 = [e for k, e in self.files_r1.items() if k in wanted or k.startswith("?")]
        self.r1_docs = [d for e in r1 for d in e["docs"]]
        self.r1_b2cs = [r for e in r1 for r in e["b2cs"]]
        self._update_state()
        self._rerun_done()

    def add_portal_files(self):
        if not self.need_client():
            return
        paths = filedialog.askopenfilenames(title="GSTR-2B or GSTR-1 JSON / ZIP (several months allowed)",
                                            filetypes=[("GST portal files", "*.json *.zip"), ("All", "*.*")])
        if paths:
            self.ingest_portal_paths(paths)

    def ingest_portal_paths(self, paths, copy=True):
        folder = config.portal_files_dir(self.s, self.client)
        dest = []
        for p in paths:
            target = p
            if copy and os.path.normcase(os.path.dirname(os.path.abspath(p))) != os.path.normcase(folder):
                target = os.path.join(folder, os.path.basename(p))
                base, ext = os.path.splitext(target)
                i = 1
                while os.path.exists(target):
                    target = f"{base}_{i}{ext}"
                    i += 1
                shutil.copy2(p, target)
            dest.append(target)
        if self.selfcheck:
            self._apply_portal_files(self._parse_portal_files(dest), merge=True)
            return
        self.run_bg("Reading portal files", lambda pr: self._parse_portal_files(dest, pr),
                    lambda ents: self._apply_portal_files(ents, merge=True))

    # ---------- assisted browser
    def _portal(self, then):
        if not self.need_client():
            return
        if self.portal and self.portal.is_alive() and self.portal_client == self.client["id"]:
            return then(self.portal)
        dl = config.portal_files_dir(self.s, self.client)
        cid, name = self.client["id"], self.client["name"]
        worker = PortalWorker(dl, config.profile_dir(self.client), self.s.get("browser", "msedge"), log=self.log,
                              on_download=lambda p: self.on_ui(lambda: self._on_download(p, cid)),
                              selectors_override=config.selectors_override())
        self.portal, self.portal_client = worker, cid
        worker.start()
        self.lbl_portal.configure(text="Starting browser…")
        self._progress_start()
        self._apply_progress("Starting browser…", None)

        def wait():
            worker.ready.wait(60)
            if worker.start_error:
                err = worker.start_error

                def failed():
                    if self.portal is worker:
                        self.portal = None
                    self.lbl_portal.configure(text="Browser not started.")
                    self._progress_done("Browser could not start")
                    self.error(str(err))
                self.on_ui(failed)
            else:
                self.on_ui(lambda: (self.lbl_portal.configure(
                    text=f"Browser running for {name}. Downloads are saved in the client's Portal files folder."),
                    then(worker)))
        threading.Thread(target=wait, daemon=True).start()

    def _on_download(self, path, cid):
        if self.client and self.client["id"] == cid:
            self.ingest_portal_paths([path], copy=False)

    def _portal_submit(self, p, jobs):
        """jobs: [(name, args, label)]. Drives the progress bar across the queued portal jobs."""
        self.portal_jobs = [0, len(jobs)]
        self._progress_start()
        self._apply_progress(f"{jobs[0][2]}…", None if len(jobs) == 1 else 0.02)
        for name, args, label in jobs:
            p.submit(name, *args, callback=self._portal_cb(label))

    def _portal_cb(self, label):
        def cb(ok, res):
            self.portal_jobs[0] += 1
            done, total = self.portal_jobs
            if not ok:
                self.log(f"{label}: {res}")
                self.on_ui(lambda: self.error(f"{label}: {res}", "GST portal"))
            if done >= total:
                self.on_ui(lambda: self._progress_done(f"{label} – finished"))
            else:
                self.progress(f"{label} – done ({done}/{total})", done / total)
        return cb

    def portal_login(self):
        self._portal(lambda p: self._portal_submit(p, [("open_login", (), "Opening the GST portal"),
                                                       ("wait_login", (), "Waiting for you to log in")]))

    def portal_2b(self):
        months = self.months()
        self._portal(lambda p: self._portal_submit(
            p, [("download_2b", (y, m), f"GSTR-2B {period_label(y, m)}") for (y, m) in months]))

    def portal_r1_download(self):
        a, b = self.periods()
        if a != b:
            return self.info("Select a single month (From = To) for GSTR-1.")
        self._portal(lambda p: self._portal_submit(p, [("download_gstr1", a, f"GSTR-1 {period_label(*a)}")]))

    def portal_r1_upload(self, path=None):
        a, b = self.periods()
        if a != b:
            return self.info("Select a single month (From = To) for GSTR-1.")
        if not self.need_client():
            return
        path = path or filedialog.askopenfilename(title="GSTR-1 JSON to upload", initialdir=self.out_dir(),
                                                  filetypes=[("JSON", "*.json")])
        if not path:
            return
        if not self.ask(f"Upload {os.path.basename(path)} for {self.client['name']}, {period_label(*a)}?\n\n"
                        f"The portal adds/overwrites these invoices in the saved GSTR-1 draft. You still review "
                        f"and file it yourself."):
            return
        self._portal(lambda p: self._portal_submit(p, [("upload_gstr1", (path, *a), "GSTR-1 upload")]))

    # ================================================================== page 3: purchase reco
    RECO_COLS = ["Status", "Party", "GSTIN", "Type", "Books inv", "Books date", "Portal inv", "Portal date",
                 "Books taxable", "Portal taxable", "Diff taxable", "Books tax", "Portal tax", "Diff tax",
                 "Remarks"]
    RECO_W = [120, 200, 135, 42, 110, 82, 110, 82, 100, 100, 90, 90, 90, 80, 440]

    def _reco_kpis(self, page, store, refill):
        row = tk.Frame(page, bg=C["bg"])
        row.pack(fill="x", pady=(0, 12))
        items = [(reco.MATCHED, "Matched"), (reco.PROBABLE, "Probable"), (reco.MISMATCH, "Amount mismatch"),
                 (reco.GSTIN_MISMATCH, "GSTIN mismatch"), (reco.BOOKS_ONLY, "Only in books"),
                 (reco.PORTAL_ONLY, "Only in portal")]
        for i, (stt, title) in enumerate(items):
            bg, fg = STATUS_STYLE[stt]
            w = Kpi(row, title, fg, bg, command=lambda s=stt: refill(s), compact=self.compact)
            w.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 8, 0))
            row.columnconfigure(i, weight=1)
            store[stt] = w
        return row

    def _page_purchase(self, page):
        self._page_head(page, "Purchase register vs GSTR-2B",
                        "Click a tile to filter. Double-click a row to compare books and portal side by side.")
        tb = self._toolbar(page)
        ttk.Button(tb, text="▶  Run reconciliation", style="Primary.TButton", command=self.run_purchase).pack(
            side="left")
        ttk.Button(tb, text="⬇  Export to Excel", command=self.export_purchase).pack(side="left", padx=8)
        tk.Label(tb, text="Show", bg=C["card"], fg=C["muted"]).pack(side="left", padx=(16, 6))
        self.v_pfilter = tk.StringVar(value="All")
        cb = ttk.Combobox(tb, textvariable=self.v_pfilter, values=["All", "Exceptions"] + reco.STATUS_ORDER,
                          width=16, state="readonly")
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", lambda e: self._refill_purchase())
        tk.Label(tb, text="Search", bg=C["card"], fg=C["muted"]).pack(side="left", padx=(16, 6))
        self.v_psearch = tk.StringVar()
        ttk.Entry(tb, textvariable=self.v_psearch, width=28).pack(side="left")
        self.v_psearch.trace_add("write", lambda *a: self._debounce("p", self._refill_purchase))
        self.lbl_itc = tk.Label(tb, text="", bg=C["card"], fg=C["red"], font=(FONT, 10, "bold"))
        self.lbl_itc.pack(side="right")
        self.kpi_p = {}
        self._reco_kpis(page, self.kpi_p, self._kpi_filter_p)
        f, self.tv_p = make_tree(page, self.RECO_COLS, self.RECO_W)
        f.pack(fill="both", expand=True)
        self.tv_p.bind("<Double-1>", lambda e: self._detail(self.tv_p, self.purchase_rows))

    def _debounce(self, key, fn, ms=300):
        attr = f"_deb_{key}"
        if getattr(self, attr, None):
            self.after_cancel(getattr(self, attr))
        setattr(self, attr, self.after(ms, fn))

    def _kpi_filter_p(self, status):
        self.v_pfilter.set("All" if self.v_pfilter.get() == status else status)
        self._refill_purchase()

    def _refill_purchase(self):
        flt = self.v_pfilter.get()
        for s_, w in self.kpi_p.items():
            w.set_active(s_ == flt)
        self._fill_reco(self.tv_p, self.purchase_rows, flt, self.v_psearch.get())

    def run_purchase(self, quiet=False):
        if not self.books_in or not self.p2b:
            if quiet:                       # data for these months not loaded (yet): clear the old result
                self.purchase_rows = []
                self._kpi_fill(self.kpi_p, [])
                self.lbl_itc.configure(text="")
                self._refill_purchase()
                return None
            if not self.books_in:
                return self.info("Load purchase data from Tally first (page 1).")
            return self.info("Download or add GSTR-2B for these months first (page 2).")
        self.ran.add("purchase")
        self.purchase_rows = reco.reconcile(self.books_in, self.p2b, float(self.s["tolerance"]))
        self._kpi_fill(self.kpi_p, self.purchase_rows)
        risk = sum(r.books.tax for r in self.purchase_rows if r.status == reco.BOOKS_ONLY and r.books) + \
            sum(max(0.0, r.diff("tax")) for r in self.purchase_rows if r.status == reco.MISMATCH)
        self.lbl_itc.configure(text=f"ITC at risk: {short_money(risk)}" if risk else "No ITC at risk")
        self._refill_purchase()
        if not quiet:
            c = reco.status_counts(self.purchase_rows)
            self.log("Purchase reco: " + ", ".join(f"{k} {c[k]}" for k in reco.STATUS_ORDER if c[k]))
            self._progress_done("Purchase reconciliation – done")

    def _kpi_fill(self, store, rows):
        summ = {s_[0]: s_ for s_ in reco.summarize(rows)}
        for stt, w in store.items():
            s_ = summ.get(stt)
            if s_:
                w.set(f"{s_[1]:,}", f"tax {short_money(s_[3] or s_[5])}")
            else:
                w.set("0" if rows else "–", "")

    def _fill_reco(self, tv, rows, flt="All", search=""):
        tv.delete(*tv.get_children())
        q = search.strip().lower()
        shown = total = 0
        for i, r in enumerate(rows):
            if flt == "Exceptions" and r.status == reco.MATCHED:
                continue
            if flt not in ("All", "Exceptions") and r.status != flt:
                continue
            b, p = r.books, r.portal
            if q:
                hay = " ".join(str(x) for x in (r.party, r.gstin, b.inv_no if b else "", p.inv_no if p else "",
                                                r.remarks)).lower()
                if q not in hay:
                    continue
            total += 1
            if shown >= MAX_SCREEN_ROWS:
                continue
            shown += 1
            tv.insert("", "end", iid=str(i), tags=(r.status,), values=[
                r.status, r.party, r.gstin, (b or p).doc_type, b.inv_no if b else "",
                fmt_date(b.inv_date) if b else "", p.inv_no if p else "", fmt_date(p.inv_date) if p else "",
                money(b.taxable) if b else "", money(p.taxable) if p else "", money(r.diff("taxable")),
                money(b.tax) if b else "", money(p.tax) if p else "", money(r.diff("tax")), r.remarks])
        if total > shown:
            self.lbl_status.configure(text=f"Showing {shown:,} of {total:,} rows – Export to Excel has all rows.")

    def _detail(self, tv, rows):
        sel = tv.selection()
        if sel:
            DetailDialog(self, rows[int(sel[0])])

    def export_purchase(self):
        if not self.purchase_rows:
            return self.info("Run the reconciliation first.")
        a, b = self.periods()
        p = os.path.join(self.out_dir(), "Purchase_vs_GSTR2B.xlsx")
        rows = self.purchase_rows
        title = (f"Purchase register vs GSTR-2B – {self.client['name']} ({self.gstin()}) – {period_label(*a)}"
                 + ("" if a == b else f" to {period_label(*b)}"))
        self.run_bg("Writing Excel", lambda pr: report.write_reco(p, rows, title, portal_label="2B"), self._opened)

    # ================================================================== page 4: sales
    def _page_sales(self, page):
        self._page_head(page, "Sales & GSTR-1", "Build the GSTR-1 upload file from Tally, or compare your books with "
                                                "the GSTR-1 already filed.")
        tb = self._toolbar(page)
        ttk.Button(tb, text="⚙  Generate GSTR-1 JSON", style="Primary.TButton", command=self.gen_gstr1).pack(
            side="left")
        ttk.Button(tb, text="⬆  Upload to portal", command=self.upload_generated).pack(side="left", padx=8)
        ttk.Button(tb, text="▶  Reco books vs filed GSTR-1", command=self.run_sales).pack(side="left")
        ttk.Button(tb, text="⬇  Export reco", command=self.export_sales).pack(side="left", padx=8)
        self.kpi_s = {}
        self._reco_kpis(page, self.kpi_s, self._kpi_filter_s)
        mid = tk.Frame(page, bg=C["bg"])
        mid.pack(fill="x", pady=(0, 12))
        f1, self.tv_r1sum = make_tree(mid, ["Section", "Docs", "Taxable", "Tax"], [210, 60, 130, 120],
                                      height=4 if self.compact else 6)
        f1.pack(side="left", fill="y")
        o, c_ = card(mid, 12, 8)
        o.pack(side="left", fill="both", expand=True, padx=(12, 0))
        tk.Label(c_, text="Notes & warnings", bg=C["card"], fg=C["maroon"], font=(FONT, 10, "bold")).pack(anchor="w")
        self.txt_r1warn = tk.Text(c_, height=4 if self.compact else 8, wrap="word", font=(FONT, 9), relief="flat", bg=C["card"],
                                  highlightthickness=0)
        self.txt_r1warn.pack(fill="both", expand=True)
        f2, self.tv_s = make_tree(page, self.RECO_COLS, self.RECO_W, height=10)
        f2.pack(fill="both", expand=True)
        self.tv_s.bind("<Double-1>", lambda e: self._detail(self.tv_s, self.sales_rows))
        self.v_sfilter = "All"

    def _kpi_filter_s(self, status):
        self.v_sfilter = "All" if self.v_sfilter == status else status
        for s_, w in self.kpi_s.items():
            w.set_active(s_ == self.v_sfilter)
        self._fill_reco(self.tv_s, self.sales_rows, self.v_sfilter)

    def gen_gstr1(self):
        if not self.need_client():
            return
        if not self.books_out:
            return self.info("Load sales data from Tally first (page 1).")
        g = self.gstin()
        self.gstr1_files = []
        self.tv_r1sum.delete(*self.tv_r1sum.get_children())
        self.txt_r1warn.delete("1.0", "end")
        for (y, m) in self.months():
            docs = [d for d in self.books_out if d.inv_date and (d.inv_date.year, d.inv_date.month) == (y, m)]
            if not docs:
                continue
            data, warns, summ = gstr1_builder.build_gstr1(docs, g, fp(y, m))
            path = os.path.join(self.out_dir(), f"GSTR1_{g}_{fp(y, m)}.json")
            gstr1_builder.save_gstr1(data, path)
            self.gstr1_files.append(((y, m), path))
            for n, s_ in enumerate(summ):
                self.tv_r1sum.insert("", "end", values=[f"{period_label(y, m)}  {s_[0]}", s_[1], money(s_[2]),
                                                       money(s_[3])], tags=("odd",) if n % 2 else ())
            self.txt_r1warn.insert("end", f"✔ {period_label(y, m)} → {path}\n")
            for w in warns:
                self.txt_r1warn.insert("end", f"   • {w}\n")
            self.log(f"GSTR-1 JSON written: {path}")
        if not self.gstr1_files:
            self.info("No sales documents in the selected months.")
        else:
            self.txt_r1warn.insert("end", "\nTip: import the JSON into the GST Offline Tool once to validate it "
                                          "before your first upload.\n")
            self._progress_done(f"GSTR-1 file{'s' if len(self.gstr1_files) > 1 else ''} generated")

    def upload_generated(self):
        a, b = self.periods()
        match = [p for (ym, p) in self.gstr1_files if ym == a]
        if a != b or not match:
            return self.info("Select a single month and click 'Generate GSTR-1 JSON' first.")
        self.portal_r1_upload(match[0])

    def run_sales(self, quiet=False):
        if not self.client or not self.books_out or not (self.r1_docs or self.r1_b2cs):
            if quiet:
                self.sales_rows, self.b2cs_rows = [], []
                self._kpi_fill(self.kpi_s, [])
                self._fill_reco(self.tv_s, [], self.v_sfilter)
                return None
            if not self.books_out:
                return self.info("Load sales data from Tally first.")
            return self.info("Download or add the filed GSTR-1 for these months (page 2).")
        self.ran.add("sales")
        g = self.gstin()
        inv_books = [d for d in self.books_out if gstr1_builder.section_of(d, g) not in ("B2CS", "NIL")]
        self.sales_rows = reco.reconcile(inv_books, self.r1_docs, float(self.s["tolerance"]))
        self.b2cs_rows = reco.reconcile_b2cs(gstr1_builder.books_b2cs(self.books_out, g),
                                             portal_data.aggregate_b2cs(self.r1_b2cs), float(self.s["tolerance"]))
        self._kpi_fill(self.kpi_s, self.sales_rows)
        self._fill_reco(self.tv_s, self.sales_rows, self.v_sfilter)
        self.tv_r1sum.delete(*self.tv_r1sum.get_children())
        for s_ in reco.summarize(self.sales_rows):
            self.tv_r1sum.insert("", "end", tags=(s_[0],) if s_[0] in STATUS_STYLE else ("bold",),
                                 values=[s_[0], s_[1], money(s_[2] - s_[4]), money(s_[3] - s_[5])])
        self.txt_r1warn.delete("1.0", "end")
        self.txt_r1warn.insert("end", "Table shows books − GSTR-1 differences.\n\nB2CS by place of supply & rate:\n")
        for r in self.b2cs_rows:
            mark = "✔" if r["status"] == reco.MATCHED else "✖"
            self.txt_r1warn.insert("end", f"  {mark} POS {r['pos']} @ {r['rate']:g}%: books "
                                          f"{money(r['books_taxable'])} vs GSTR-1 {money(r['portal_taxable'])}\n")
        if not quiet:
            c = reco.status_counts(self.sales_rows)
            self.log("Sales reco: " + ", ".join(f"{k} {c[k]}" for k in reco.STATUS_ORDER if c[k]))
            self._progress_done("Sales reconciliation – done")

    def export_sales(self):
        if not self.sales_rows:
            return self.info("Run the sales reconciliation first.")
        p = os.path.join(self.out_dir(), "Sales_vs_GSTR1.xlsx")
        rows, b2 = self.sales_rows, self.b2cs_rows
        title = f"Sales register vs GSTR-1 – {self.client['name']} ({self.gstin()})"
        self.run_bg("Writing Excel", lambda pr: report.write_reco(p, rows, title, b2, "GSTR-1"), self._opened)

    # ================================================================== page 5: 3B
    def _page_3b(self, page):
        self._page_head(page, "GSTR-3B working", "Built from books (page 1), GSTR-2B (page 2) and the purchase "
                                                 "reconciliation (page 3) if you ran it.")
        tb = self._toolbar(page)
        ttk.Button(tb, text="▶  Compute GSTR-3B", style="Primary.TButton", command=self.run_3b).pack(side="left")
        ttk.Button(tb, text="⬇  Export to Excel", command=self.export_3b).pack(side="left", padx=8)
        f, self.tv_3b = make_tree(page, ["Table", "Description", "Taxable value", "IGST", "CGST", "SGST", "Cess"],
                                  [90, 560, 140, 120, 120, 120, 100], height=20, sort=False)
        f.pack(fill="both", expand=True)

    def run_3b(self, quiet=False):
        if not self.client or (not self.books_out and not self.p2b):
            if quiet:
                self.rows3b = []
                self.tv_3b.delete(*self.tv_3b.get_children())
                return None
            return self.info("Load Tally data and GSTR-2B first.")
        self.ran.add("3b")
        self.rows3b = gstr3b.compute_3b(self.books_out, self.books_in, self.p2b, self.purchase_rows or None,
                                        self.gstin())
        tv = self.tv_3b
        tv.delete(*tv.get_children())
        for n, (sec, label, v) in enumerate(self.rows3b):
            tag = ("bold",) if sec in ("Net", "Safe ITC", "Cash") else (("odd",) if n % 2 else ())
            tv.insert("", "end", tags=tag, values=[sec, label, money(v["taxable"]), money(v["igst"]),
                                                   money(v["cgst"]), money(v["sgst"]), money(v["cess"])])
        if not quiet:
            self._progress_done("GSTR-3B working computed")

    def export_3b(self):
        if not self.rows3b:
            return self.info("Compute GSTR-3B first.")
        a, b = self.periods()
        p = os.path.join(self.out_dir(), "GSTR3B_working.xlsx")
        rows = self.rows3b
        title = (f"GSTR-3B working – {self.client['name']} ({self.gstin()}) – {period_label(*a)}"
                 + ("" if a == b else f" to {period_label(*b)}"))
        self.run_bg("Writing Excel", lambda pr: report.write_3b(p, rows, title), self._opened)

    # ================================================================== settings
    def _page_settings(self, page):
        self._page_head(page, "Settings", "These apply to all clients. Client details are under Manage clients.")
        o, c_ = card(page, 20, 16)
        o.pack(fill="x")
        self.v_host = tk.StringVar(value=self.s["tally_host"])
        self.v_port = tk.StringVar(value=str(self.s["tally_port"]))
        self.v_tol = tk.StringVar(value=str(self.s["tolerance"]))
        self.v_work = tk.StringVar(value=self.s["work_dir"])
        self.v_browser = tk.StringVar(value=self.s.get("browser", "msedge"))
        self.v_upd = tk.StringVar(value=self.s.get("update_url_override", ""))
        rows = [("Tally host", ttk.Entry(c_, textvariable=self.v_host, width=30)),
                ("Tally port", ttk.Entry(c_, textvariable=self.v_port, width=10)),
                ("Matching tolerance (₹)", ttk.Entry(c_, textvariable=self.v_tol, width=10)),
                ("Main work folder", ttk.Entry(c_, textvariable=self.v_work, width=70)),
                ("Browser for the portal", ttk.Combobox(c_, textvariable=self.v_browser, values=["msedge", "chrome"],
                                                        width=12, state="readonly")),
                ("Update address (optional)", ttk.Entry(c_, textvariable=self.v_upd, width=70))]
        for i, (lbl, w) in enumerate(rows):
            tk.Label(c_, text=lbl, bg=C["card"], fg=C["text"], font=(FONT, 10)).grid(row=i, column=0, sticky="w",
                                                                                     pady=6, padx=(0, 16))
            w.grid(row=i, column=1, sticky="w")
        ttk.Button(c_, text="Browse…", command=lambda: self.v_work.set(
            filedialog.askdirectory() or self.v_work.get())).grid(row=3, column=2, padx=8)
        tk.Label(c_, text=f"blank = built-in: {UPDATE_URL or 'none'}", bg=C["card"], fg=C["muted"],
                 font=(FONT, 8)).grid(row=6, column=1, sticky="w")
        bt = tk.Frame(c_, bg=C["card"])
        bt.grid(row=7, column=1, sticky="w", pady=(14, 0))
        ttk.Button(bt, text="Save settings", style="Primary.TButton", command=self._save_settings).pack(side="left")
        tk.Label(page, text=(f"Settings file: {config.SETTINGS_PATH}\nPortal selector overrides (optional): "
                             f"{config.selectors_override()}\nEach client gets its own folder inside the main work "
                             f"folder, and its own browser profile for the GST portal."),
                 bg=C["bg"], fg=C["muted"], font=(FONT, 9), justify="left").pack(anchor="w", pady=12)

    def _save_settings(self, quiet=False):
        try:
            if hasattr(self, "v_host"):
                self.s.update({"tally_host": self.v_host.get().strip(), "tally_port": int(self.v_port.get()),
                               "tolerance": float(self.v_tol.get()), "work_dir": self.v_work.get().strip(),
                               "browser": self.v_browser.get(), "update_url_override": self.v_upd.get().strip()})
            config.save(self.s)
            if not quiet:
                self._progress_done("Settings saved")
        except ValueError as e:
            if not quiet:
                self.error(f"Invalid value: {e}", "Settings")

    # ================================================================== about / updates
    def about(self):
        d = tk.Toplevel(self)
        d.title(f"About {APP_NAME}")
        d.configure(bg=C["card"])
        d.resizable(False, False)
        d.transient(self)
        if self._images.get("logo"):
            tk.Label(d, image=self._images["logo"], bg=C["card"]).pack(padx=40, pady=(24, 8))
        tk.Label(d, text=f"{APP_NAME}  {__version__}", bg=C["card"], fg=C["maroon"], font=(FONT, 14, "bold")).pack()
        tk.Label(d, text="Tally ↔ GST portal reconciliation", bg=C["card"], fg=C["muted"]).pack()
        tk.Label(d, text=f"Developed and owned by\n{OWNER}\nAll rights reserved.", bg=C["card"], fg=C["text"],
                 font=(FONT, 9), justify="center").pack(pady=14)
        ttk.Button(d, text="Close", style="Primary.TButton", command=d.destroy).pack(pady=(0, 20))
        return d

    def update_url(self):
        return (self.s.get("update_url_override") or "").strip() or UPDATE_URL

    def check_updates(self, silent=False):
        url = self.update_url()
        if not url:
            if not silent:
                self.info("No update address is set for this copy.")
            return
        self.btn_update.state(["disabled"])
        self.btn_update.configure(text="Checking…")

        def work():
            try:
                m = updater.check_for_update(url)
                self.on_ui(lambda: self._update_found(m, silent))
            except updater.UpdateError as e:
                self.log(str(e))
                self.on_ui(lambda: (self._reset_update_btn(), None if silent else self.error(str(e), "Updates")))
        threading.Thread(target=work, daemon=True).start()

    def _reset_update_btn(self):
        self.btn_update.state(["!disabled"])
        self.btn_update.configure(text="⟳  Check for updates")

    def _update_found(self, m, silent):
        self._reset_update_btn()
        if not m:
            self.log(f"{APP_NAME} {__version__} is up to date.")
            if not silent:
                self.info(f"You have the latest version ({__version__}).", "Updates")
            return
        self.btn_update.configure(text=f"⬆  Update to {m['version']}")
        UpdateDialog(self, m)

    def _quit_for_update(self):
        if self.portal:
            self.portal.stop()
        self.destroy()
        os._exit(0)

    def _opened(self, path):
        self.log(f"Saved: {path}")
        if self.selfcheck:
            return
        try:
            os.startfile(path)  # type: ignore[attr-defined]  (Windows)
        except (AttributeError, OSError):
            pass


# ================================================================== dialogs
class Dialog(tk.Toplevel):
    def __init__(self, app, title):
        super().__init__(app)
        self.app = app
        self.title(title)
        self.configure(bg=C["bg"])
        self.transient(app)
        head = tk.Frame(self, bg=C["maroon"], padx=18, pady=10)
        head.pack(fill="x")
        tk.Label(head, text=title, bg=C["maroon"], fg="white", font=(FONT, 13, "bold")).pack(anchor="w")
        tk.Frame(self, bg=C["yellow"], height=px(3)).pack(fill="x")
        self.body = tk.Frame(self, bg=C["bg"], padx=18, pady=14)
        self.body.pack(fill="both", expand=True)

    def center(self):
        self.update_idletasks()
        x = self.app.winfo_rootx() + (self.app.winfo_width() - self.winfo_width()) // 2
        y = self.app.winfo_rooty() + (self.app.winfo_height() - self.winfo_height()) // 3
        self.geometry(f"+{max(0, x)}+{max(0, y)}")


class ClientsDialog(Dialog):
    def __init__(self, app, first_run=False):
        super().__init__(app, "Manage clients")
        self.s = app.s
        self.editing = None
        if first_run:
            tk.Label(self.body, text="Welcome! Add your first client (company) to get started.", bg=C["bg"],
                     fg=C["maroon"], font=(FONT, 11, "bold")).pack(anchor="w", pady=(0, 10))
        main = tk.Frame(self.body, bg=C["bg"])
        main.pack(fill="both", expand=True)
        left = tk.Frame(main, bg=C["bg"])
        left.pack(side="left", fill="both", expand=True)
        f, self.tv = make_tree(left, ["Client", "GSTIN", "Tally company"], [200, 140, 220], height=12)
        f.pack(fill="both", expand=True)
        self.tv.bind("<<TreeviewSelect>>", lambda e: self._load_selected())
        tk.Label(left, text="Removing a client only removes it from this list – its folder and files stay.",
                 bg=C["bg"], fg=C["muted"], font=(FONT, 8)).pack(anchor="w", pady=(6, 0))

        o, form = card(main, 16, 14)
        o.pack(side="left", fill="y", padx=(14, 0))
        self.lbl_mode = tk.Label(form, text="New client", bg=C["card"], fg=C["maroon"], font=(FONT, 11, "bold"))
        self.lbl_mode.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        self.v_name, self.v_gstin, self.v_comp = tk.StringVar(), tk.StringVar(), tk.StringVar()
        tk.Label(form, text="Client name", bg=C["card"]).grid(row=1, column=0, sticky="w", pady=5)
        ttk.Entry(form, textvariable=self.v_name, width=34).grid(row=1, column=1, sticky="w")
        tk.Label(form, text="GSTIN", bg=C["card"]).grid(row=2, column=0, sticky="w", pady=5)
        gf = tk.Frame(form, bg=C["card"])
        gf.grid(row=2, column=1, sticky="w")
        ttk.Entry(gf, textvariable=self.v_gstin, width=22, font=("Consolas", 10)).pack(side="left")
        self.lbl_gok = tk.Label(gf, text="", bg=C["card"], font=(FONT, 9, "bold"))
        self.lbl_gok.pack(side="left", padx=6)
        self.v_gstin.trace_add("write", lambda *a: self._check_gstin())
        tk.Label(form, text="Tally company", bg=C["card"]).grid(row=3, column=0, sticky="w", pady=5)
        self.cb_comp = ttk.Combobox(form, textvariable=self.v_comp, width=32)
        self.cb_comp.grid(row=3, column=1, sticky="w")
        ttk.Button(form, text="⟳  List companies open in Tally", style="Link.TButton",
                   command=self._list_companies).grid(row=4, column=1, sticky="w")
        btns = tk.Frame(form, bg=C["card"])
        btns.grid(row=5, column=0, columnspan=2, sticky="w", pady=(14, 0))
        ttk.Button(btns, text="Save", style="Primary.TButton", command=self._save).pack(side="left")
        ttk.Button(btns, text="+  New", command=self._new).pack(side="left", padx=6)
        ttk.Button(btns, text="Remove", command=self._delete).pack(side="left")
        ttk.Button(btns, text="Use this client", style="Accent.TButton", command=self._use).pack(side="left", padx=6)
        bottom = tk.Frame(self.body, bg=C["bg"])
        bottom.pack(fill="x", pady=(12, 0))
        ttk.Button(bottom, text="Close", command=self.destroy).pack(side="right")
        self._fill()
        if app.client:
            self._fill(select=app.client["id"])
        else:
            self._new()
        self.center()
        if not app.selfcheck:
            self.grab_set()

    def _fill(self, select=None):
        self.tv.delete(*self.tv.get_children())
        for n, c in enumerate(self.s["clients"]):
            self.tv.insert("", "end", iid=c["id"], values=[c["name"], c.get("gstin", ""), c.get("tally_company", "")],
                           tags=("odd",) if n % 2 else ())
        if select and self.tv.exists(select):
            self.tv.selection_set(select)
            self.tv.see(select)
            self._load(select)

    def _check_gstin(self):
        g = self.v_gstin.get().strip().upper()
        if not g:
            self.lbl_gok.configure(text="")
        elif is_valid_gstin(g):
            self.lbl_gok.configure(text="✔ valid", fg=C["green"])
        else:
            self.lbl_gok.configure(text="✖ check", fg=C["red"])

    def _new(self):
        self.editing = None
        sel = self.tv.selection()
        if sel:
            self.tv.selection_remove(*sel)
        self.lbl_mode.configure(text="New client")
        for v in (self.v_name, self.v_gstin, self.v_comp):
            v.set("")

    def _load_selected(self):
        sel = self.tv.selection()
        if sel:
            self._load(sel[0])

    def _load(self, cid):
        c = next((x for x in self.s["clients"] if x["id"] == cid), None)
        if c:
            self.editing = c["id"]
            self.lbl_mode.configure(text=f"Edit: {c['name']}")
            self.v_name.set(c["name"])
            self.v_gstin.set(c.get("gstin", ""))
            self.v_comp.set(c.get("tally_company", ""))

    def _save(self):
        name, g, comp = self.v_name.get().strip(), self.v_gstin.get().strip().upper(), self.v_comp.get().strip()
        if not name:
            return messagebox.showwarning("Clients", "Enter a client name.", parent=self)
        if not is_valid_gstin(g):
            return messagebox.showwarning("Clients", "Enter a valid 15-character GSTIN.", parent=self)
        if any(c["name"].lower() == name.lower() and c["id"] != self.editing for c in self.s["clients"]):
            return messagebox.showwarning("Clients", "A client with this name already exists.", parent=self)
        if self.editing:
            c = next(x for x in self.s["clients"] if x["id"] == self.editing)
            c.update(name=name, gstin=g, tally_company=comp)
        else:
            c = config.new_client(name, g, comp)
            self.s["clients"].append(c)
            self.editing = c["id"]
        first = not self.app.client
        self.app.clients_changed(select_id=c["id"] if first else None)
        self._fill(select=c["id"])

    def _delete(self):
        if not self.editing:
            return
        c = next(x for x in self.s["clients"] if x["id"] == self.editing)
        if not messagebox.askyesno("Clients", f"Remove '{c['name']}' from the list?\n\nIts folder and files are "
                                              f"not deleted.", parent=self):
            return
        self.s["clients"] = [x for x in self.s["clients"] if x["id"] != self.editing]
        self._fill()
        self._new()
        self.app.clients_changed()

    def _use(self):
        if not self.editing:
            self._save()
        if self.editing:
            self.app.select_client(self.editing)
            self.destroy()

    def _list_companies(self):
        c = tally.TallyClient(self.app.s["tally_host"], self.app.s["tally_port"], timeout=15)

        def work():
            try:
                names = c.list_companies()
                self.app.on_ui(lambda: self._got_companies(names))
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                self.app.on_ui(lambda: self.winfo_exists() and messagebox.showwarning("Tally", msg, parent=self))
        threading.Thread(target=work, daemon=True).start()

    def _got_companies(self, names):
        if not self.winfo_exists():
            return
        self.cb_comp["values"] = names
        if names and not self.v_comp.get():
            self.v_comp.set(names[0])
        messagebox.showinfo("Tally", "Companies open in Tally:\n\n" + ("\n".join(names) or "none"), parent=self)


class CheckDialog(Dialog):
    def __init__(self, app, title, results):
        super().__init__(app, title)
        o, c_ = card(self.body, 16, 12)
        o.pack(fill="both")
        for ok, msg in results:
            sym, col = ("✔", C["green"]) if ok else (("✖", C["red"]) if ok is False else ("ℹ", C["purple"]))
            row = tk.Frame(c_, bg=C["card"])
            row.pack(fill="x", pady=3)
            tk.Label(row, text=sym, bg=C["card"], fg=col, font=(FONT, 12, "bold"), width=2).pack(side="left",
                                                                                              anchor="n")
            tk.Label(row, text=msg, bg=C["card"], fg=C["text"], font=(FONT, 10), wraplength=px(620),
                     justify="left", anchor="w").pack(side="left", fill="x")
        for ok, msg in results:
            app.log(("OK   " if ok else "FAIL " if ok is False else "INFO ") + msg)
        ttk.Button(self.body, text="Close", style="Primary.TButton", command=self.destroy).pack(anchor="e",
                                                                                               pady=(12, 0))
        self.center()


class DetailDialog(Dialog):
    FIELDS = [("Document", "doc_type"), ("Invoice no.", "inv_no"), ("Date", "inv_date"), ("Party", "party"),
              ("GSTIN", "gstin"), ("Place of supply", "pos"), ("Taxable", "taxable"), ("IGST", "igst"),
              ("CGST", "cgst"), ("SGST", "sgst"), ("Cess", "cess"), ("Total tax", "tax"), ("Invoice value", "total"),
              ("Rates", "rates"), ("Voucher", "voucher_no"), ("ITC available (2B)", "itc_avail")]

    def __init__(self, app, row):
        super().__init__(app, row.status)
        bg, fg = STATUS_STYLE.get(row.status, (C["card"], C["text"]))
        tk.Label(self.body, text=row.remarks or "No remarks.", bg=bg, fg=fg, font=(FONT, 10, "bold"),
                 wraplength=px(620), justify="left", padx=10, pady=8, anchor="w").pack(fill="x", pady=(0, 10))
        o, g = card(self.body, 0, 0)
        o.pack(fill="both")
        for c_, h in enumerate(["", "Books (Tally)", "Portal", "Difference"]):
            tk.Label(g, text=h, bg=C["maroon"], fg="white", font=(FONT, 9, "bold"), padx=10, pady=6,
                     anchor="w", width=22 if c_ else 18).grid(row=0, column=c_, sticky="ew")

        def val(d, attr):
            if d is None:
                return None
            if attr == "rates":
                return ", ".join(f"{x.rate:g}%" for x in d.rate_lines)
            v = getattr(d, attr)
            return fmt_date(v) if attr == "inv_date" else v

        for r_, (label, attr) in enumerate(self.FIELDS, start=1):
            b, p = val(row.books, attr), val(row.portal, attr)
            num = isinstance(b, float) or isinstance(p, float)
            diff = ""
            if num and b is not None and p is not None:
                diff = money(b - p)
            elif not num and b is not None and p is not None and str(b) != str(p) and \
                    attr not in ("party", "voucher_no", "itc_avail"):
                diff = "differs"
            bgc = C["stripe"] if r_ % 2 else C["card"]
            for c_, t in enumerate([label, money(b) if num and b is not None else (b or "—"),
                                    money(p) if num and p is not None else (p or "—"), diff]):
                hot = c_ == 3 and diff and diff not in ("0.00", "-0.00")
                tk.Label(g, text=t, bg=bgc, fg=C["red"] if hot else C["text"], padx=10, pady=4, anchor="w",
                         font=(FONT, 9, "bold") if c_ == 0 or hot else (FONT, 9)).grid(row=r_, column=c_, sticky="ew")
        ttk.Button(self.body, text="Close", style="Primary.TButton", command=self.destroy).pack(anchor="e",
                                                                                               pady=(12, 0))
        self.center()


class UpdateDialog(Dialog):
    def __init__(self, app, m):
        super().__init__(app, "Update available")
        self.m = m
        tk.Label(self.body, text=f"{APP_NAME} {m['version']} is available (you have {__version__}).", bg=C["bg"],
                 fg=C["text"], font=(FONT, 11, "bold")).pack(anchor="w")
        notes = tk.Text(self.body, height=10, width=70, wrap="word", font=(FONT, 9), relief="flat",
                        highlightthickness=1, highlightbackground=C["border"], padx=8, pady=6)
        notes.insert("1.0", m.get("notes") or "Bug fixes and improvements.")
        notes.configure(state="disabled")
        notes.pack(fill="both", pady=10)
        self.pb = ttk.Progressbar(self.body, style="Brand.Horizontal.TProgressbar", length=px(480), mode="determinate")
        self.pb.pack(fill="x")
        self.lbl = tk.Label(self.body, text="Your clients, settings and reports are kept. The app will close and "
                                            "reopen.", bg=C["bg"], fg=C["muted"])
        self.lbl.pack(anchor="w", pady=(6, 10))
        b = tk.Frame(self.body, bg=C["bg"])
        b.pack(fill="x")
        self.b_now = ttk.Button(b, text="⬆  Update now", style="Primary.TButton", command=self.go)
        self.b_now.pack(side="right")
        self.b_later = ttk.Button(b, text="Later", command=self.destroy)
        self.b_later.pack(side="right", padx=8)
        self.center()
        self.grab_set()

    def go(self):
        app = self.app
        if not updater.is_installed_app():
            messagebox.showinfo("Updates", "You are running from source code. Install the new version with the "
                                           "installer instead.", parent=self)
            return
        self.b_now.state(["disabled"])
        self.b_later.state(["disabled"])
        app._save_settings(quiet=True)

        def prog(done, total):
            app.on_ui(lambda: (self.pb.configure(maximum=total or 1, value=done),
                               self.lbl.configure(text=f"Downloading… {done / 1e6:.1f} MB"
                                                  + (f" of {total / 1e6:.1f} MB" if total else ""))))

        def work():
            try:
                path = updater.download(self.m, prog)
                app.on_ui(lambda: self.lbl.configure(text="Installing… the app will reopen shortly."))
                updater.run_installer(path)
                app.on_ui(lambda: app.after(800, app._quit_for_update))
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                app.on_ui(lambda: (messagebox.showerror("Update failed", msg, parent=self),
                                   self.b_now.state(["!disabled"]), self.b_later.state(["!disabled"])))
        threading.Thread(target=work, daemon=True).start()


# ================================================================== build-time UI self-check
def run_selfcheck(shot_dir):
    """Loads sample data through every page, saves screenshots, exits non-zero on any error."""
    root_dir = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.join(root_dir, "tests"))
    import make_sample
    errors = []
    shot_dir = os.path.abspath(shot_dir)
    os.makedirs(shot_dir, exist_ok=True)
    sample = make_sample.write_sample(tempfile.mkdtemp(prefix="gstreconx_sample_"))
    s = config.load()
    s["work_dir"] = tempfile.mkdtemp(prefix="gstreconx_work_")
    s["clients"] = [config.new_client("Sample Traders", make_sample.COMPANY, "Sample Traders Pvt Ltd"),
                    config.new_client("Second Client LLP", make_sample.SUP_B, "Second Client LLP")]
    s["current_client"] = s["clients"][0]["id"]
    config.save(s)

    app = App(selfcheck=True)
    app.report_callback_exception = lambda *a: errors.append("".join(traceback.format_exception(*a)))
    sw, sh = app.winfo_screenwidth(), app.winfo_screenheight()
    app.geometry(f"{min(1360, sw - 20)}x{min(860, sh - 60)}+0+0")

    def pump(t=0.3):
        end = time.time() + t
        while time.time() < end:
            app.update()
            time.sleep(0.02)

    def shot(name, widget=None):
        pump(0.6)
        try:
            from PIL import ImageGrab
            w = widget or app
            w.lift()
            pump(0.2)
            x, y = w.winfo_rootx(), w.winfo_rooty()
            ImageGrab.grab(bbox=(x, y, x + w.winfo_width(), y + w.winfo_height())).save(
                os.path.join(shot_dir, f"ui-preview-{name}.png"))
        except Exception as e:  # noqa: BLE001
            print("screenshot skipped:", e)

    try:
        app.attributes("-topmost", True)
        pump(0.5)
        app.select_client(s["clients"][0]["id"])
        pump(0.5)
        app.v_from.set("Oct-2026")
        app.v_to.set("Oct-2026")
        app.books_source = "files"
        app._set_books(tally.load_from_files([os.path.join(sample, "tally_masters.xml"),
                                              os.path.join(sample, "tally_vouchers.xml")],
                                             make_sample.COMPANY, *period_bounds((2026, 10), (2026, 10))))
        app._progress_start()
        app._apply_progress("Sample data loaded", 0.65)
        shot("1-tally")
        app.ingest_portal_paths([os.path.join(sample, "GSTR2B_102026.json")])
        app.show_page("portal")
        shot("2-portal")
        app.show_page("purchase")
        app.run_purchase()
        assert app.purchase_rows, "purchase reco produced no rows"
        shot("3-purchase")
        app._kpi_filter_p(reco.PROBABLE)
        app._kpi_filter_p(reco.PROBABLE)
        DetailDialog(app, app.purchase_rows[0]).destroy()
        app.show_page("sales")
        app.gen_gstr1()
        assert app.gstr1_files, "GSTR-1 JSON not generated"
        app.ingest_portal_paths([app.gstr1_files[0][1]])
        app.run_sales()
        assert app.sales_rows, "sales reco produced no rows"
        shot("4-sales")
        app.show_page("3b")
        app.run_3b()
        assert app.rows3b, "3B produced no rows"
        shot("5-gstr3b")
        from gstreco import tally as _t
        res = _t.diagnose(_t.TallyClient("127.0.0.1", 9, timeout=3), "X", make_sample.COMPANY, date(2026, 10, 1))
        assert res and res[0][0] is False, "connection test must report a missing Tally"
        CheckDialog(app, "Tally connection test", res).destroy()
        app.show_page("settings")
        pump(0.3)
        dlg = app.manage_clients()
        shot("6-clients", dlg)
        dlg.destroy()
        ab = app.about()
        shot("7-about", ab)
        ab.destroy()
        app._period_changed()
        app.select_client(s["clients"][1]["id"])
        pump(0.5)
        assert not app.books_out and not app.p2b, "switching client must clear data"
        app.select_client(s["clients"][0]["id"])
        pump(0.5)
        assert app.p2b and app.r1_docs, "saved portal files must reload for the client"
        app.show_page("purchase")
        shot("8-after-switch")
    except Exception:  # noqa: BLE001
        errors.append(traceback.format_exc())
    finally:
        try:
            app.destroy()
        except Exception:  # noqa: BLE001
            pass
    if errors:
        print("UI SELF-CHECK FAILED")
        for e in errors:
            print(e)
        sys.exit(1)
    print("UI SELF-CHECK PASSED; screenshots in", shot_dir)


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        i = sys.argv.index("--selfcheck")
        run_selfcheck(sys.argv[i + 1] if len(sys.argv) > i + 1 else "ui-preview")
    else:
        App().mainloop()
