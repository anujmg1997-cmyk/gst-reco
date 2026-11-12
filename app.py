"""GST Reco – desktop app (Windows). Tally ↔ GST portal reconciliation.

Run:  python app.py
"""
import os
import queue
import threading
import traceback
from datetime import date

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import sys

from gstreco import UPDATE_URL, __version__, config, gstr1_builder, gstr3b, portal_data, reco, report, tally
from gstreco import updater
from gstreco.normalize import (fmt_date, fp, is_valid_gstin, months_between, parse_period_label,
                               period_bounds, period_label)
from gstreco.portal import PortalWorker

STATUS_TAGS = {
    reco.MATCHED: "#E2F0D9", reco.PROBABLE: "#FFF2CC", reco.MISMATCH: "#FCE4D6",
    reco.GSTIN_MISMATCH: "#FCE4D6", reco.BOOKS_ONLY: "#F8CBAD", reco.PORTAL_ONLY: "#DDEBF7",
}


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


def resource(*parts):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, *parts)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"GST Reco {__version__} – Tally ↔ GST portal")
        self.geometry("1280x800")
        self.minsize(1000, 640)
        self.s = config.load()
        self.ui_q = queue.Queue()
        self.portal = None

        self.books_out, self.books_in = [], []
        self.p2b, self.p2b_files = [], []
        self.r1_docs, self.r1_b2cs, self.r1_file = [], [], None
        self.purchase_rows, self.sales_rows, self.b2cs_rows, self.rows3b = [], [], [], []
        self.gstr1_files = []

        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Treeview", rowheight=22)
        style.configure("Big.TButton", padding=(10, 5))
        try:
            self._icon = tk.PhotoImage(file=resource("assets", "app.png"))
            self.iconphoto(True, self._icon)
        except tk.TclError:
            pass

        self._menu()
        self._header()
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=8, pady=(0, 4))
        self._tab_tally()
        self._tab_portal()
        self._tab_purchase()
        self._tab_sales()
        self._tab_3b()
        self._tab_settings()
        self._logbox()
        self.after(100, self._drain)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        if updater.is_installed_app() and self.update_url():
            self.after(4000, lambda: self.check_updates(silent=True))

    # ================================================================== plumbing
    def log(self, msg):
        self.ui_q.put(("log", msg))

    def _drain(self):
        try:
            while True:
                kind, payload = self.ui_q.get_nowait()
                if kind == "log":
                    self.logtxt.configure(state="normal")
                    self.logtxt.insert("end", payload + "\n")
                    self.logtxt.see("end")
                    self.logtxt.configure(state="disabled")
                elif kind == "call":
                    payload()
        except queue.Empty:
            pass
        self.after(100, self._drain)

    def on_ui(self, fn):
        self.ui_q.put(("call", fn))

    def run_bg(self, label, fn, done=None):
        self.log(f"{label}…")
        self.config(cursor="watch")

        def worker():
            try:
                res = fn()
                self.on_ui(lambda: (self.config(cursor=""), done and done(res)))
            except Exception as e:  # noqa: BLE001
                tb = traceback.format_exc()
                self.log(tb)
                self.on_ui(lambda: (self.config(cursor=""), messagebox.showerror("GST Reco", str(e))))
        threading.Thread(target=worker, daemon=True).start()

    def _on_close(self):
        self._save_settings(quiet=True)
        if self.portal:
            self.portal.stop()
        self.destroy()

    # ================================================================== header
    def _header(self):
        f = ttk.Frame(self, padding=(8, 8, 8, 4))
        f.pack(fill="x")
        ttk.Label(f, text="Your GSTIN").grid(row=0, column=0, sticky="w")
        self.v_gstin = tk.StringVar(value=self.s["gstin"])
        ttk.Entry(f, textvariable=self.v_gstin, width=18).grid(row=0, column=1, padx=(4, 16))
        choices = period_choices()
        default = choices[1] if len(choices) > 1 else choices[0]
        ttk.Label(f, text="From").grid(row=0, column=2)
        self.v_from = tk.StringVar(value=default)
        ttk.Combobox(f, textvariable=self.v_from, values=choices, width=10, state="readonly").grid(
            row=0, column=3, padx=4)
        ttk.Label(f, text="To").grid(row=0, column=4)
        self.v_to = tk.StringVar(value=default)
        ttk.Combobox(f, textvariable=self.v_to, values=choices, width=10, state="readonly").grid(
            row=0, column=5, padx=(4, 16))
        ttk.Label(f, text="Tally company").grid(row=0, column=6)
        self.v_company = tk.StringVar(value=self.s["tally_company"])
        self.cb_company = ttk.Combobox(f, textvariable=self.v_company, width=34)
        self.cb_company.grid(row=0, column=7, padx=4)
        ttk.Button(f, text="↻ Companies", command=self.refresh_companies).grid(row=0, column=8)
        f.columnconfigure(9, weight=1)
        self.btn_update = ttk.Button(f, text="⟳ Check for updates", command=self.check_updates)
        self.btn_update.grid(row=0, column=10, sticky="e")
        ttk.Label(f, text=f"v{__version__}", foreground="#777").grid(row=1, column=10, sticky="e")
        self.lbl_state = ttk.Label(f, text="", foreground="#555")
        self.lbl_state.grid(row=1, column=0, columnspan=9, sticky="w", pady=(6, 0))
        self._update_state()

    def _update_state(self):
        n2b = len(self.p2b)
        self.lbl_state.config(text=(
            f"Books: {len(self.books_out)} sales docs, {len(self.books_in)} purchase docs   |   "
            f"GSTR-2B: {n2b} docs from {len(self.p2b_files)} file(s)   |   "
            f"Filed GSTR-1: {len(self.r1_docs)} docs{' (' + os.path.basename(self.r1_file) + ')' if self.r1_file else ''}"))

    def periods(self):
        a, b = parse_period_label(self.v_from.get()), parse_period_label(self.v_to.get())
        if a > b:
            a, b = b, a
        return a, b

    def gstin(self):
        g = self.v_gstin.get().strip().upper()
        if not is_valid_gstin(g):
            raise ValueError("Enter a valid GSTIN (your own) at the top.")
        return g

    def out_dir(self):
        a, b = self.periods()
        tag = period_label(*a) + ("" if a == b else "_to_" + period_label(*b))
        d = os.path.join(self.s["work_dir"], self.v_gstin.get().strip().upper() or "NOGSTIN", tag)
        os.makedirs(d, exist_ok=True)
        return d

    # ================================================================== widgets
    def _tree(self, parent, cols, widths, height=18):
        frame = ttk.Frame(parent)
        tv = ttk.Treeview(frame, columns=cols, show="headings", height=height)
        for c, w in zip(cols, widths):
            tv.heading(c, text=c, command=lambda c=c: self._sort(tv, c))
            anchor = "e" if any(k in c.lower() for k in ("taxable", "igst", "cgst", "sgst", "cess",
                                                           "tax", "total", "diff", "value")) else "w"
            tv.column(c, width=w, anchor=anchor, stretch=False)
        ys = ttk.Scrollbar(frame, orient="vertical", command=tv.yview)
        xs = ttk.Scrollbar(frame, orient="horizontal", command=tv.xview)
        tv.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        tv.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        for st, color in STATUS_TAGS.items():
            tv.tag_configure(st, background=color)
        tv.tag_configure("warn", background="#FFF2CC")
        return frame, tv

    @staticmethod
    def _sort(tv, col):
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

    def _bar(self, parent):
        b = ttk.Frame(parent, padding=(0, 6))
        b.pack(fill="x")
        return b

    def _logbox(self):
        f = ttk.LabelFrame(self, text="Activity", padding=4)
        f.pack(fill="x", padx=8, pady=(0, 8))
        self.logtxt = tk.Text(f, height=7, state="disabled", wrap="word", font=("Consolas", 9))
        self.logtxt.pack(fill="x")

    # ================================================================== tab 1: Tally
    def _tab_tally(self):
        t = ttk.Frame(self.nb, padding=8)
        self.nb.add(t, text="1. Tally data")
        b = self._bar(t)
        ttk.Button(b, text="Fetch from Tally", style="Big.TButton", command=self.fetch_tally).pack(side="left")
        ttk.Button(b, text="Load Tally XML files…", command=self.load_tally_files).pack(side="left", padx=6)
        ttk.Button(b, text="Export books to Excel", command=self.export_books).pack(side="left")
        self.lbl_tally = ttk.Label(b, text="TallyPrime must be open with the company loaded and the "
                                           "HTTP server on (F1 > Settings > Connectivity, port 9000).",
                                   foreground="#555")
        self.lbl_tally.pack(side="left", padx=12)
        cols = ["Dir", "Type", "Date", "Voucher", "Inv no", "Party", "GSTIN", "POS", "Taxable", "IGST",
                "CGST", "SGST", "Cess", "Total", "Rates", "Warnings"]
        f, self.tv_books = self._tree(t, cols, [45, 45, 85, 90, 120, 200, 140, 40, 100, 85, 85, 85, 60, 100,
                                                70, 400])
        f.pack(fill="both", expand=True)

    def refresh_companies(self):
        c = tally.TallyClient(self.s["tally_host"], self.s["tally_port"], timeout=20)

        def done(names):
            self.cb_company["values"] = names
            if names and self.v_company.get() not in names:
                self.v_company.set(names[0])
            self.log(f"Tally companies open: {', '.join(names) or 'none'}")
        self.run_bg("Connecting to Tally", c.list_companies, done)

    def fetch_tally(self):
        try:
            g = self.gstin()
        except ValueError as e:
            return messagebox.showwarning("GST Reco", str(e))
        a, b = self.periods()
        d_from, d_to = period_bounds(a, b)
        company = self.v_company.get().strip()
        c = tally.TallyClient(self.s["tally_host"], self.s["tally_port"])
        cache = os.path.join(self.out_dir(), "tally_raw")
        self.run_bg(f"Fetching vouchers {fmt_date(d_from)} to {fmt_date(d_to)} from Tally",
                    lambda: tally.fetch_from_tally(c, company, g, d_from, d_to, cache), self._set_books)

    def load_tally_files(self):
        paths = filedialog.askopenfilenames(title="Tally XML (masters + vouchers / Day Book)",
                                            filetypes=[("XML", "*.xml"), ("All", "*.*")])
        if not paths:
            return
        try:
            g = self.gstin()
        except ValueError as e:
            return messagebox.showwarning("GST Reco", str(e))
        d_from, d_to = period_bounds(*self.periods())
        self.run_bg("Reading Tally XML", lambda: tally.load_from_files(paths, g, d_from, d_to), self._set_books)

    def _set_books(self, res):
        self.books_out, self.books_in = res
        tv = self.tv_books
        tv.delete(*tv.get_children())
        for d in self.books_out + self.books_in:
            tv.insert("", "end", tags=("warn",) if d.warnings else (), values=[
                "Sale" if d.direction == "out" else "Purch", d.doc_type, fmt_date(d.inv_date), d.voucher_no,
                d.inv_no, d.party, d.gstin, d.pos, money(d.taxable), money(d.igst), money(d.cgst),
                money(d.sgst), money(d.cess), money(d.total), ", ".join(f"{r.rate:g}%" for r in d.rate_lines),
                "; ".join(d.warnings)])
        nw = sum(1 for d in self.books_out + self.books_in if d.warnings)
        self.log(f"Books loaded: {len(self.books_out)} sales/credit notes, {len(self.books_in)} purchases/"
                 f"debit notes. {nw} with warnings (highlighted).")
        self._update_state()

    def export_books(self):
        if not (self.books_out or self.books_in):
            return messagebox.showinfo("GST Reco", "Fetch Tally data first.")
        p = os.path.join(self.out_dir(), "Books_GST_register.xlsx")
        report.write_books(p, self.books_out + self.books_in, "Books")
        self._opened(p)

    # ================================================================== tab 2: Portal
    def _tab_portal(self):
        t = ttk.Frame(self.nb, padding=8)
        self.nb.add(t, text="2. GST portal")
        box = ttk.LabelFrame(t, text="Assisted portal (opens Edge/Chrome – you enter CAPTCHA & OTP)", padding=8)
        box.pack(fill="x")
        ttk.Button(box, text="Open portal & log in", style="Big.TButton", command=self.portal_login).grid(
            row=0, column=0, padx=4, pady=4, sticky="ew")
        ttk.Button(box, text="Download GSTR-2B (From–To months)", command=self.portal_2b).grid(
            row=0, column=1, padx=4, pady=4, sticky="ew")
        ttk.Button(box, text="Download filed GSTR-1", command=self.portal_r1_download).grid(
            row=0, column=2, padx=4, pady=4, sticky="ew")
        ttk.Button(box, text="Upload GSTR-1 JSON…", command=self.portal_r1_upload).grid(
            row=0, column=3, padx=4, pady=4, sticky="ew")
        box2 = ttk.LabelFrame(t, text="Or load files you downloaded yourself", padding=8)
        box2.pack(fill="x", pady=8)
        ttk.Button(box2, text="Load GSTR-2B JSON/ZIP…", command=self.load_2b).grid(row=0, column=0, padx=4)
        ttk.Button(box2, text="Clear 2B", command=self.clear_2b).grid(row=0, column=1, padx=4)
        ttk.Button(box2, text="Load filed GSTR-1 JSON/ZIP…", command=self.load_r1).grid(row=0, column=2, padx=4)
        help_ = (
            "How it works\n"
            "• 'Open portal & log in' opens a browser window. Log in yourself – username, password, CAPTCHA and "
            "OTP are never stored or typed by the app.\n"
            "• The download/upload buttons then pick the financial year and month on the Returns Dashboard and "
            "click the right buttons. Any file downloaded in that window is saved to your work folder and loaded "
            "automatically.\n"
            "• If the portal layout has changed and a click fails, the app tells you which button to click "
            "yourself – the file is still captured.\n"
            "• Uploading GSTR-1 attaches the JSON under Prepare Offline > Upload. After the portal processes it, "
            "review the summary and file with DSC/EVC yourself – the app never clicks the final File button.")
        msg = tk.Message(t, text=help_, width=1100)
        msg.pack(fill="x", pady=8, anchor="w")
        self.lbl_portal = ttk.Label(t, text="Browser not started.", foreground="#555")
        self.lbl_portal.pack(anchor="w")

    def _portal(self, then):
        if self.portal and self.portal.is_alive():
            return then(self.portal)
        dl = os.path.join(self.s["work_dir"], "downloads")
        self.portal = PortalWorker(dl, config.profile_dir(), self.s.get("browser", "msedge"), log=self.log,
                                   on_download=lambda p: self.on_ui(lambda: self._auto_load(p)),
                                   selectors_override=config.selectors_override())
        self.portal.start()
        self.lbl_portal.config(text="Starting browser…")

        def wait():
            self.portal.ready.wait(60)
            if self.portal.start_error:
                err = self.portal.start_error
                self.portal = None
                self.on_ui(lambda: (self.lbl_portal.config(text="Browser not started."),
                                    messagebox.showerror("GST Reco", str(err))))
            else:
                self.on_ui(lambda: (self.lbl_portal.config(text=f"Browser running. Downloads go to {dl}"),
                                    then(self.portal)))
        threading.Thread(target=wait, daemon=True).start()

    def _portal_cb(self, label):
        def cb(ok, res):
            if not ok:
                self.log(f"{label}: {res}")
                self.on_ui(lambda: messagebox.showwarning("GST portal", f"{label}: {res}"))
        return cb

    def portal_login(self):
        def go(p):
            p.submit("open_login", callback=self._portal_cb("Open portal"))
            p.submit("wait_login", callback=self._portal_cb("Login"))
        self._portal(go)

    def portal_2b(self):
        months = months_between(*self.periods())

        def go(p):
            for (y, m) in months:
                p.submit("download_2b", y, m, callback=self._portal_cb(f"GSTR-2B {period_label(y, m)}"))
        self._portal(go)

    def portal_r1_download(self):
        a, b = self.periods()
        if a != b:
            return messagebox.showinfo("GST Reco", "Select a single month (From = To) for GSTR-1.")

        def go(p):
            p.submit("download_gstr1", *a, callback=self._portal_cb("GSTR-1 download"))
        self._portal(go)

    def portal_r1_upload(self, path=None):
        a, b = self.periods()
        if a != b:
            return messagebox.showinfo("GST Reco", "Select a single month (From = To) for GSTR-1.")
        path = path or filedialog.askopenfilename(title="GSTR-1 JSON to upload", initialdir=self.out_dir(),
                                                  filetypes=[("JSON", "*.json")])
        if not path:
            return
        if not messagebox.askyesno("Upload GSTR-1", f"Upload {os.path.basename(path)} for "
                                   f"{period_label(*a)} to the GST portal?\n\nThe portal will add/overwrite "
                                   f"these invoices in your saved GSTR-1 draft. You still file it yourself."):
            return

        def go(p):
            p.submit("upload_gstr1", path, *a, callback=self._portal_cb("GSTR-1 upload"))
        self._portal(go)

    def _auto_load(self, path):
        low = os.path.basename(path).lower()
        try:
            docs, meta = portal_data.load_gstr2b(path)
            if docs or "2b" in low:
                self.p2b += docs
                self.p2b_files.append(path)
                self.log(f"GSTR-2B loaded: {len(docs)} docs from {os.path.basename(path)}")
                self._update_state()
                return
            r1, b2cs, _ = portal_data.load_gstr1(path)
            if r1 or b2cs:
                self.r1_docs, self.r1_b2cs, self.r1_file = r1, b2cs, path
                self.log(f"Filed GSTR-1 loaded: {len(r1)} docs, {len(b2cs)} B2CS rows")
                self._update_state()
        except Exception as e:  # noqa: BLE001
            self.log(f"Saved {os.path.basename(path)} (not auto-loaded: {e})")

    def load_2b(self):
        paths = filedialog.askopenfilenames(title="GSTR-2B JSON or ZIP (select several months if needed)",
                                            filetypes=[("GSTR-2B", "*.json *.zip"), ("All", "*.*")])
        for p in paths:
            docs, meta = portal_data.load_gstr2b(p)
            self.p2b += docs
            self.p2b_files.append(p)
            self.log(f"GSTR-2B {', '.join(meta['periods']) or ''}: {len(docs)} docs from {os.path.basename(p)}")
        self._update_state()

    def clear_2b(self):
        self.p2b, self.p2b_files = [], []
        self._update_state()

    def load_r1(self):
        p = filedialog.askopenfilename(title="Filed GSTR-1 JSON/ZIP", filetypes=[("GSTR-1", "*.json *.zip")])
        if p:
            self.r1_docs, self.r1_b2cs, _ = portal_data.load_gstr1(p)
            self.r1_file = p
            self.log(f"Filed GSTR-1 loaded: {len(self.r1_docs)} docs, {len(self.r1_b2cs)} B2CS rows")
            self._update_state()

    # ================================================================== tab 3: purchase reco
    RECO_COLS = ["Status", "Party", "GSTIN", "Type", "Books inv", "Books date", "Portal inv", "Portal date",
                 "Books taxable", "Portal taxable", "Diff taxable", "Books tax", "Portal tax", "Diff tax",
                 "Remarks"]
    RECO_W = [115, 190, 135, 40, 110, 80, 110, 80, 100, 100, 90, 90, 90, 80, 420]

    def _tab_purchase(self):
        t = ttk.Frame(self.nb, padding=8)
        self.nb.add(t, text="3. Purchase vs GSTR-2B")
        b = self._bar(t)
        ttk.Button(b, text="Run reconciliation", style="Big.TButton", command=self.run_purchase).pack(side="left")
        ttk.Label(b, text="  Show").pack(side="left")
        self.v_pfilter = tk.StringVar(value="All")
        cb = ttk.Combobox(b, textvariable=self.v_pfilter, values=["All", "Exceptions"] + reco.STATUS_ORDER,
                          width=16, state="readonly")
        cb.pack(side="left", padx=4)
        cb.bind("<<ComboboxSelected>>", lambda e: self._fill_reco(self.tv_p, self.purchase_rows,
                                                                  self.v_pfilter.get()))
        ttk.Button(b, text="Export to Excel", command=self.export_purchase).pack(side="left", padx=6)
        f1, self.tv_psum = self._tree(t, ["Status", "Count", "Books taxable", "Books tax", "2B taxable",
                                          "2B tax"], [160, 60, 130, 120, 130, 120], height=7)
        f1.pack(fill="x")
        f2, self.tv_p = self._tree(t, self.RECO_COLS, self.RECO_W)
        f2.pack(fill="both", expand=True, pady=(6, 0))
        self.tv_p.bind("<Double-1>", lambda e: self._detail(self.tv_p, self.purchase_rows))

    def run_purchase(self):
        if not self.books_in:
            return messagebox.showinfo("GST Reco", "Fetch purchase data from Tally first (tab 1).")
        if not self.p2b:
            return messagebox.showinfo("GST Reco", "Load or download GSTR-2B first (tab 2).")
        self.purchase_rows = reco.reconcile(self.books_in, self.p2b, float(self.s["tolerance"]))
        self._fill_summary(self.tv_psum, self.purchase_rows)
        self._fill_reco(self.tv_p, self.purchase_rows, self.v_pfilter.get())
        c = reco.status_counts(self.purchase_rows)
        self.log("Purchase reco: " + ", ".join(f"{k} {c[k]}" for k in reco.STATUS_ORDER if c[k]))

    def _fill_summary(self, tv, rows):
        tv.delete(*tv.get_children())
        for s in reco.summarize(rows):
            tv.insert("", "end", tags=(s[0],), values=[s[0], s[1]] + [money(x) for x in s[2:]])

    def _fill_reco(self, tv, rows, flt="All"):
        tv.delete(*tv.get_children())
        tv._rows = []
        for i, r in enumerate(rows):
            if flt == "Exceptions" and r.status == reco.MATCHED:
                continue
            if flt not in ("All", "Exceptions") and r.status != flt:
                continue
            b, p = r.books, r.portal
            tv.insert("", "end", iid=str(i), tags=(r.status,), values=[
                r.status, r.party, r.gstin, (b or p).doc_type, b.inv_no if b else "",
                fmt_date(b.inv_date) if b else "", p.inv_no if p else "", fmt_date(p.inv_date) if p else "",
                money(b.taxable) if b else "", money(p.taxable) if p else "", money(r.diff("taxable")),
                money(b.tax) if b else "", money(p.tax) if p else "", money(r.diff("tax")), r.remarks])

    def _detail(self, tv, rows):
        sel = tv.selection()
        if not sel:
            return
        r = rows[int(sel[0])]

        def side(d, label):
            if not d:
                return f"{label}: —"
            return (f"{label}: {d.doc_type} {d.inv_no} dt {fmt_date(d.inv_date)}  {d.party}  {d.gstin}\n"
                    f"   Taxable {money(d.taxable)}  IGST {money(d.igst)}  CGST {money(d.cgst)}  "
                    f"SGST {money(d.sgst)}  Cess {money(d.cess)}  Total {money(d.total)}\n"
                    f"   Rates: {', '.join(f'{x.rate:g}%' for x in d.rate_lines)}"
                    + (f"\n   Voucher {d.voucher_type} {d.voucher_no}" if d.voucher_no else "")
                    + (f"\n   Warnings: {'; '.join(d.warnings)}" if d.warnings else ""))
        messagebox.showinfo(r.status, f"{side(r.books, 'BOOKS')}\n\n{side(r.portal, 'PORTAL')}\n\n{r.remarks}")

    def export_purchase(self):
        if not self.purchase_rows:
            return messagebox.showinfo("GST Reco", "Run the reconciliation first.")
        a, b = self.periods()
        p = os.path.join(self.out_dir(), "Purchase_vs_GSTR2B.xlsx")
        report.write_reco(p, self.purchase_rows, f"Purchase register vs GSTR-2B – {self.v_gstin.get()} – "
                                                 f"{period_label(*a)} to {period_label(*b)}", portal_label="2B")
        self._opened(p)

    # ================================================================== tab 4: sales
    def _tab_sales(self):
        t = ttk.Frame(self.nb, padding=8)
        self.nb.add(t, text="4. Sales & GSTR-1")
        b = self._bar(t)
        ttk.Button(b, text="Generate GSTR-1 JSON", style="Big.TButton", command=self.gen_gstr1).pack(side="left")
        ttk.Button(b, text="Upload generated JSON to portal", command=self.upload_generated).pack(side="left",
                                                                                                padx=6)
        ttk.Button(b, text="Reco books vs filed GSTR-1", command=self.run_sales).pack(side="left")
        ttk.Button(b, text="Export reco to Excel", command=self.export_sales).pack(side="left", padx=6)
        pw = ttk.PanedWindow(t, orient="vertical")
        pw.pack(fill="both", expand=True)
        top = ttk.Frame(pw)
        f1, self.tv_r1sum = self._tree(top, ["Section", "Docs", "Taxable", "Tax"], [200, 60, 140, 120], height=6)
        f1.pack(side="left", fill="y")
        self.txt_r1warn = tk.Text(top, height=8, wrap="word", font=("Segoe UI", 9))
        self.txt_r1warn.pack(side="left", fill="both", expand=True, padx=(8, 0))
        pw.add(top, weight=1)
        bottom = ttk.Frame(pw)
        f2, self.tv_s = self._tree(bottom, self.RECO_COLS, self.RECO_W)
        f2.pack(fill="both", expand=True)
        self.tv_s.bind("<Double-1>", lambda e: self._detail(self.tv_s, self.sales_rows))
        pw.add(bottom, weight=3)

    def gen_gstr1(self):
        try:
            g = self.gstin()
        except ValueError as e:
            return messagebox.showwarning("GST Reco", str(e))
        if not self.books_out:
            return messagebox.showinfo("GST Reco", "Fetch sales data from Tally first (tab 1).")
        self.gstr1_files = []
        self.tv_r1sum.delete(*self.tv_r1sum.get_children())
        self.txt_r1warn.delete("1.0", "end")
        for (y, m) in months_between(*self.periods()):
            docs = [d for d in self.books_out if d.inv_date and (d.inv_date.year, d.inv_date.month) == (y, m)]
            if not docs:
                continue
            data, warns, summ = gstr1_builder.build_gstr1(docs, g, fp(y, m))
            path = os.path.join(self.out_dir(), f"GSTR1_{g}_{fp(y, m)}.json")
            gstr1_builder.save_gstr1(data, path)
            self.gstr1_files.append(((y, m), path))
            for s in summ:
                self.tv_r1sum.insert("", "end", values=[f"{period_label(y, m)} {s[0]}", s[1], money(s[2]),
                                                       money(s[3])])
            self.txt_r1warn.insert("end", f"{period_label(y, m)} → {path}\n")
            for w in warns:
                self.txt_r1warn.insert("end", f"  • {w}\n")
            self.log(f"GSTR-1 JSON written: {path}")
        if not self.gstr1_files:
            messagebox.showinfo("GST Reco", "No sales documents in the selected period.")
        else:
            self.txt_r1warn.insert("end", "\nTip: import the JSON into the GST Offline Tool once to validate it "
                                          "before your first upload.\n")

    def upload_generated(self):
        a, b = self.periods()
        match = [p for (ym, p) in self.gstr1_files if ym == a]
        if a != b or not match:
            return messagebox.showinfo("GST Reco", "Select a single month and click 'Generate GSTR-1 JSON' first.")
        self.portal_r1_upload(match[0])

    def run_sales(self):
        try:
            g = self.gstin()
        except ValueError as e:
            return messagebox.showwarning("GST Reco", str(e))
        if not self.books_out:
            return messagebox.showinfo("GST Reco", "Fetch sales data from Tally first.")
        if not (self.r1_docs or self.r1_b2cs):
            return messagebox.showinfo("GST Reco", "Load or download the filed GSTR-1 first (tab 2).")
        inv_books = [d for d in self.books_out if gstr1_builder.section_of(d, g) not in ("B2CS", "NIL")]
        self.sales_rows = reco.reconcile(inv_books, self.r1_docs, float(self.s["tolerance"]))
        self.b2cs_rows = reco.reconcile_b2cs(gstr1_builder.books_b2cs(self.books_out, g),
                                             portal_data.aggregate_b2cs(self.r1_b2cs), float(self.s["tolerance"]))
        self._fill_reco(self.tv_s, self.sales_rows)
        self.tv_r1sum.delete(*self.tv_r1sum.get_children())
        for s in reco.summarize(self.sales_rows):
            self.tv_r1sum.insert("", "end", tags=(s[0],), values=[s[0], s[1], money(s[2] - s[4]),
                                                                 money(s[3] - s[5])])
        self.txt_r1warn.delete("1.0", "end")
        self.txt_r1warn.insert("end", "Summary shows books − GSTR-1 differences.\n\nB2CS (by POS & rate):\n")
        for r in self.b2cs_rows:
            self.txt_r1warn.insert("end", f"  POS {r['pos']} @ {r['rate']:g}%: {r['status']} – books "
                                          f"{money(r['books_taxable'])} vs GSTR-1 {money(r['portal_taxable'])}\n")
        c = reco.status_counts(self.sales_rows)
        self.log("Sales reco: " + ", ".join(f"{k} {c[k]}" for k in reco.STATUS_ORDER if c[k]))

    def export_sales(self):
        if not self.sales_rows:
            return messagebox.showinfo("GST Reco", "Run the sales reconciliation first.")
        p = os.path.join(self.out_dir(), "Sales_vs_GSTR1.xlsx")
        report.write_reco(p, self.sales_rows, f"Sales register vs GSTR-1 – {self.v_gstin.get()}",
                          self.b2cs_rows, "GSTR-1")
        self._opened(p)

    # ================================================================== tab 5: 3B
    def _tab_3b(self):
        t = ttk.Frame(self.nb, padding=8)
        self.nb.add(t, text="5. GSTR-3B")
        b = self._bar(t)
        ttk.Button(b, text="Compute GSTR-3B working", style="Big.TButton", command=self.run_3b).pack(side="left")
        ttk.Button(b, text="Export to Excel", command=self.export_3b).pack(side="left", padx=6)
        ttk.Label(b, text="Uses books (tab 1), GSTR-2B (tab 2) and the purchase reco (tab 3) if run.",
                  foreground="#555").pack(side="left", padx=12)
        f, self.tv_3b = self._tree(t, ["Table", "Description", "Taxable value", "IGST", "CGST", "SGST", "Cess"],
                                   [80, 560, 130, 110, 110, 110, 90], height=22)
        f.pack(fill="both", expand=True)

    def run_3b(self):
        try:
            g = self.gstin()
        except ValueError as e:
            return messagebox.showwarning("GST Reco", str(e))
        if not self.books_out and not self.p2b:
            return messagebox.showinfo("GST Reco", "Load Tally data and GSTR-2B first.")
        self.rows3b = gstr3b.compute_3b(self.books_out, self.books_in, self.p2b,
                                        self.purchase_rows or None, g)
        tv = self.tv_3b
        tv.delete(*tv.get_children())
        for sec, label, v in self.rows3b:
            tv.insert("", "end", values=[sec, label, money(v["taxable"]), money(v["igst"]), money(v["cgst"]),
                                         money(v["sgst"]), money(v["cess"])])
        self.log("GSTR-3B working computed.")

    def export_3b(self):
        if not self.rows3b:
            return messagebox.showinfo("GST Reco", "Compute GSTR-3B first.")
        a, b = self.periods()
        p = os.path.join(self.out_dir(), "GSTR3B_working.xlsx")
        report.write_3b(p, self.rows3b, f"GSTR-3B working – {self.v_gstin.get()} – {period_label(*a)}"
                                        + ("" if a == b else f" to {period_label(*b)}"))
        self._opened(p)

    # ================================================================== tab 6: settings
    def _tab_settings(self):
        t = ttk.Frame(self.nb, padding=12)
        self.nb.add(t, text="Settings")
        self.v_host = tk.StringVar(value=self.s["tally_host"])
        self.v_port = tk.StringVar(value=str(self.s["tally_port"]))
        self.v_tol = tk.StringVar(value=str(self.s["tolerance"]))
        self.v_work = tk.StringVar(value=self.s["work_dir"])
        self.v_browser = tk.StringVar(value=self.s.get("browser", "msedge"))
        rows = [("Tally host", self.v_host), ("Tally port", self.v_port),
                ("Matching tolerance (₹)", self.v_tol), ("Work folder", self.v_work)]
        for i, (lbl, var) in enumerate(rows):
            ttk.Label(t, text=lbl).grid(row=i, column=0, sticky="w", pady=4)
            ttk.Entry(t, textvariable=var, width=60).grid(row=i, column=1, sticky="w", padx=8)
        ttk.Button(t, text="Browse…", command=lambda: self.v_work.set(
            filedialog.askdirectory() or self.v_work.get())).grid(row=3, column=2)
        ttk.Label(t, text="Browser").grid(row=4, column=0, sticky="w", pady=4)
        ttk.Combobox(t, textvariable=self.v_browser, values=["msedge", "chrome"], width=12,
                     state="readonly").grid(row=4, column=1, sticky="w", padx=8)
        self.v_upd = tk.StringVar(value=self.s.get("update_url_override", ""))
        ttk.Label(t, text="Update address (optional)").grid(row=5, column=0, sticky="w", pady=4)
        ttk.Entry(t, textvariable=self.v_upd, width=60).grid(row=5, column=1, sticky="w", padx=8)
        ttk.Label(t, text=f"blank = built-in: {UPDATE_URL or 'none'}", foreground="#777").grid(
            row=6, column=1, sticky="w", padx=8)
        ttk.Button(t, text="Save settings", command=self._save_settings).grid(row=7, column=1, sticky="w",
                                                                            padx=8, pady=12)
        ttk.Button(t, text="Open work folder", command=lambda: self._opened(self.s["work_dir"])).grid(
            row=7, column=1, padx=130, sticky="w")
        tk.Message(t, width=900, text=(
            f"Settings file: {config.SETTINGS_PATH}\n"
            f"Portal selector overrides (optional): {config.selectors_override()}\n"
            "If the GST portal changes its layout, copy gstreco/portal_selectors.json there and edit the "
            "selectors – no rebuild needed.")).grid(row=8, column=0, columnspan=3, sticky="w", pady=12)

    def _save_settings(self, quiet=False):
        try:
            self.s.update({"gstin": self.v_gstin.get().strip().upper(), "tally_host": self.v_host.get().strip(),
                           "tally_port": int(self.v_port.get()), "tally_company": self.v_company.get(),
                           "tolerance": float(self.v_tol.get()), "work_dir": self.v_work.get().strip(),
                           "browser": self.v_browser.get(),
                           "update_url_override": self.v_upd.get().strip()})
            config.save(self.s)
            if not quiet:
                self.log("Settings saved.")
        except ValueError as e:
            if not quiet:
                messagebox.showerror("Settings", f"Invalid value: {e}")

    # ================================================================== menu & updates
    def _menu(self):
        mb = tk.Menu(self)
        m_file = tk.Menu(mb, tearoff=0)
        m_file.add_command(label="Open work folder", command=lambda: self._opened(self.s["work_dir"]))
        m_file.add_separator()
        m_file.add_command(label="Exit", command=self._on_close)
        mb.add_cascade(label="File", menu=m_file)
        m_help = tk.Menu(mb, tearoff=0)
        m_help.add_command(label="Check for updates…", command=self.check_updates)
        m_help.add_command(label="About GST Reco", command=lambda: messagebox.showinfo(
            "About", f"GST Reco {__version__}\nTally ↔ GST portal reconciliation\n\n"
                     f"Settings: {config.APP_DIR}"))
        mb.add_cascade(label="Help", menu=m_help)
        self.config(menu=mb)

    def update_url(self):
        return (self.s.get("update_url_override") or "").strip() or UPDATE_URL

    def check_updates(self, silent=False):
        url = self.update_url()
        if not url:
            if not silent:
                messagebox.showinfo("Updates", "No update address is set for this copy.\n"
                                               "Install the app from the official installer, or set the "
                                               "address under Settings.")
            return
        self.btn_update.config(state="disabled", text="Checking…")

        def work():
            try:
                m = updater.check_for_update(url)
                self.on_ui(lambda: self._update_found(m, silent))
            except updater.UpdateError as e:
                self.log(str(e))
                self.on_ui(lambda: (self._reset_update_btn(),
                                    None if silent else messagebox.showwarning("Updates", str(e))))
        threading.Thread(target=work, daemon=True).start()

    def _reset_update_btn(self):
        self.btn_update.config(state="normal", text="⟳ Check for updates")

    def _update_found(self, m, silent):
        self._reset_update_btn()
        if not m:
            self.log(f"GST Reco {__version__} is up to date.")
            if not silent:
                messagebox.showinfo("Updates", f"You have the latest version ({__version__}).")
            return
        self.btn_update.config(text=f"⬆ Update to {m['version']}")
        self._update_dialog(m)

    def _update_dialog(self, m):
        dlg = tk.Toplevel(self)
        dlg.title("Update available")
        dlg.transient(self)
        dlg.resizable(False, False)
        frm = ttk.Frame(dlg, padding=16)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text=f"GST Reco {m['version']} is available (you have {__version__}).",
                  font=("Segoe UI", 11, "bold")).pack(anchor="w")
        notes = tk.Text(frm, height=10, width=70, wrap="word", font=("Segoe UI", 9))
        notes.insert("1.0", m.get("notes") or "Bug fixes and improvements.")
        notes.configure(state="disabled")
        notes.pack(fill="both", pady=10)
        pb = ttk.Progressbar(frm, length=480, mode="determinate")
        pb.pack(fill="x")
        lbl = ttk.Label(frm, text="Your settings and reports are kept. The app will close and reopen.",
                        foreground="#555")
        lbl.pack(anchor="w", pady=(6, 10))
        btns = ttk.Frame(frm)
        btns.pack(fill="x")

        def later():
            dlg.destroy()

        def go():
            if not updater.is_installed_app():
                messagebox.showinfo("Updates", "You are running from source code. Install the new version "
                                               "with the installer instead.", parent=dlg)
                return
            b_now.config(state="disabled")
            b_later.config(state="disabled")
            self._save_settings(quiet=True)

            def prog(done, total):
                self.on_ui(lambda: (pb.configure(maximum=total or 1, value=done),
                                    lbl.config(text=f"Downloading… {done / 1e6:.1f} MB"
                                               + (f" of {total / 1e6:.1f} MB" if total else ""))))

            def work():
                try:
                    path = updater.download(m, prog)
                    self.on_ui(lambda: lbl.config(text="Installing… the app will reopen shortly."))
                    updater.run_installer(path)
                    self.on_ui(lambda: self.after(800, self._quit_for_update))
                except Exception as e:  # noqa: BLE001
                    self.on_ui(lambda: (messagebox.showerror("Update failed", str(e), parent=dlg),
                                        b_now.config(state="normal"), b_later.config(state="normal")))
            threading.Thread(target=work, daemon=True).start()

        b_now = ttk.Button(btns, text="Update now", style="Big.TButton", command=go)
        b_now.pack(side="right")
        b_later = ttk.Button(btns, text="Later", command=later)
        b_later.pack(side="right", padx=8)
        dlg.grab_set()

    def _quit_for_update(self):
        if self.portal:
            self.portal.stop()
        self.destroy()
        os._exit(0)

    # ================================================================== misc
    def _opened(self, path):
        self.log(f"Saved: {path}")
        try:
            os.startfile(path)  # type: ignore[attr-defined]  (Windows)
        except (AttributeError, OSError):
            pass


if __name__ == "__main__":
    App().mainloop()
