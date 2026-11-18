"""Assisted GST-portal browser (Playwright).

You log in yourself (username, password, CAPTCHA, OTP) in the browser window the app opens.
The app then navigates to the returns dashboard, picks the period, clicks Download / Upload,
and captures every file downloaded in that window. It never clicks the final
'File with DSC/EVC' button - you review and file yourself.

All Playwright calls run on one worker thread (the sync API is single-threaded).
"""
import json
import os
import queue
import re
import threading
import time

from .normalize import MONTH_FULL, fy_label

SEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "portal_selectors.json")


class PortalError(Exception):
    pass


def load_selectors(override_path=None):
    with open(SEL_PATH, encoding="utf-8") as f:
        sel = json.load(f)
    if override_path and os.path.exists(override_path):
        with open(override_path, encoding="utf-8") as f:
            sel.update(json.load(f))
    return sel


class PortalWorker(threading.Thread):
    def __init__(self, download_dir, profile_dir, channel="msedge", log=print, on_download=None,
                 selectors_override=None):
        super().__init__(daemon=True)
        self.download_dir, self.profile_dir, self.channel = download_dir, profile_dir, channel
        self.log, self.on_download = log, on_download
        self.sel = load_selectors(selectors_override)
        self.jobs = queue.Queue()
        self.pending = []
        self.saved = []
        self.ready = threading.Event()
        self.start_error = None
        self.ctx = self.page = None
        os.makedirs(download_dir, exist_ok=True)
        os.makedirs(profile_dir, exist_ok=True)

    # ------------------------------------------------------------ thread plumbing
    def submit(self, name, *args, callback=None):
        self.jobs.put((getattr(self, "_" + name), args, callback))

    def stop(self):
        self.jobs.put(None)

    def run(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.start_error = PortalError("Playwright is not installed. Run: pip install playwright")
            self.ready.set()
            return
        try:
            with sync_playwright() as pw:
                self.ctx = self._launch(pw)
                self.ctx.on("page", self._hook)
                for p in self.ctx.pages:
                    self._hook(p)
                self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
                self.ready.set()
                while True:
                    try:
                        job = self.jobs.get(timeout=0.4)
                    except queue.Empty:
                        self._pump(300)
                        continue
                    if job is None:
                        break
                    fn, args, cb = job
                    try:
                        res, ok = fn(*args), True
                    except Exception as e:  # noqa: BLE001
                        res, ok = e, False
                    if cb:
                        cb(ok, res)
                try:
                    self.ctx.close()
                except Exception:  # noqa: BLE001
                    pass
        except Exception as e:  # noqa: BLE001
            self.start_error = e
            self.ready.set()

    def _launch(self, pw):
        errors = []
        for ch in dict.fromkeys([self.channel, "msedge", "chrome", None]):
            try:
                kw = dict(headless=False, accept_downloads=True, no_viewport=True,
                          args=["--start-maximized"])
                if ch:
                    kw["channel"] = ch
                return pw.chromium.launch_persistent_context(self.profile_dir, **kw)
            except Exception as e:  # noqa: BLE001
                errors.append(f"{ch or 'bundled chromium'}: {str(e).splitlines()[0]}")
        raise PortalError("Could not start Edge/Chrome. " + " | ".join(errors))

    def _hook(self, page):
        page.on("download", lambda d: self.pending.append(d))

    def _active_page(self):
        pages = [p for p in self.ctx.pages if not p.is_closed()]
        if not pages:
            self.page = self.ctx.new_page()
            self._hook(self.page)
        elif self.page is None or self.page.is_closed():
            self.page = pages[-1]
        return self.page

    def _pump(self, ms):
        """Let Playwright process events (downloads) and save captured files."""
        try:
            self._active_page().wait_for_timeout(ms)
        except Exception:  # noqa: BLE001
            time.sleep(ms / 1000)
        while self.pending:
            d = self.pending.pop(0)
            try:
                name = d.suggested_filename or f"download_{int(time.time())}"
                path = os.path.join(self.download_dir, name)
                base, ext = os.path.splitext(path)
                i = 1
                while os.path.exists(path):
                    path = f"{base}_{i}{ext}"
                    i += 1
                d.save_as(path)
                self.saved.append(path)
                self.log(f"Downloaded: {os.path.basename(path)}")
                if self.on_download:
                    self.on_download(path)
            except Exception as e:  # noqa: BLE001
                self.log(f"Could not save download: {e}")

    def _wait_new_download(self, timeout_s):
        n = len(self.saved)
        end = time.time() + timeout_s
        while time.time() < end:
            self._pump(500)
            if len(self.saved) > n:
                return self.saved[n:]
        return []

    # ------------------------------------------------------------ page helpers
    def _click(self, key, timeout_ms=8000):
        page = self._active_page()
        for sel in self.sel.get(key, []):
            try:
                loc = page.locator(sel).first
                loc.wait_for(state="visible", timeout=timeout_ms)
                loc.click()
                page.wait_for_timeout(800)
                return True
            except Exception:  # noqa: BLE001
                continue
        return False

    def _select(self, key, pattern):
        page = self._active_page()
        for sel in self.sel.get(key, []):
            try:
                loc = page.locator(sel).first
                loc.wait_for(state="visible", timeout=5000)
                labels = [t.strip() for t in loc.locator("option").all_inner_texts()]
                match = next((t for t in labels if re.search(pattern, t, re.I)), None)
                if match:
                    loc.select_option(label=match)
                    page.wait_for_timeout(1200)
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    def _is_logged_in(self):
        marks = self.sel["logged_in_url_contains"]
        return any(any(m in p.url for m in marks) for p in self.ctx.pages if not p.is_closed())

    # ------------------------------------------------------------ jobs
    def _open_login(self):
        page = self._active_page()
        page.bring_to_front()
        page.goto(self.sel["login_url"], wait_until="domcontentloaded")
        self.log("Portal opened. Log in yourself (username, password, CAPTCHA, OTP).")
        return True

    def _wait_login(self, timeout_s=600):
        end = time.time() + timeout_s
        while time.time() < end:
            if self._is_logged_in():
                for p in self.ctx.pages:
                    if any(m in p.url for m in self.sel["logged_in_url_contains"]):
                        self.page = p
                self.log("Logged in to GST portal.")
                return True
            self._pump(1000)
        raise PortalError("Login not detected within 10 minutes.")

    def _ensure_login(self):
        if not self._is_logged_in():
            self._open_login()
            self._wait_login()

    def _dashboard(self, y, m):
        self._ensure_login()
        page = self._active_page()
        page.goto(self.sel["dashboard_url"], wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        q = ((m - 4) % 12) // 3 + 1
        ok = self._select("fy_select", re.escape(fy_label(y, m)))
        self._select("quarter_select", rf"Quarter\s*{q}\b")
        ok = self._select("period_select", rf"^{MONTH_FULL[m - 1]}") and ok
        if ok and self._click("search_button"):
            page.wait_for_timeout(2500)
            return True
        self.log(f"Please select FY {fy_label(y, m)} / {MONTH_FULL[m - 1]} and click SEARCH in the browser.")
        return False

    def _download_2b(self, y, m):
        self._dashboard(y, m)
        clicked = self._click("gstr2b_download", 10000) and self._click("gstr2b_generate_json", 15000)
        if clicked:
            self.log(f"GSTR-2B {MONTH_FULL[m - 1]} {y}: JSON requested, waiting for download…")
        else:
            self.log("Could not find the GSTR-2B buttons automatically. In the browser click "
                     "GSTR-2B > DOWNLOAD > 'Generate JSON file to download'. The file will be picked up.")
        files = self._wait_new_download(120 if clicked else 300)
        if not files:
            raise PortalError("No GSTR-2B file received (2B may not be generated yet for this period).")
        return files

    def _upload_gstr1(self, path, y, m):
        self._dashboard(y, m)
        page = self._active_page()
        step = self._click("gstr1_prepare_offline", 10000) and self._click("offline_upload_tab")
        if step:
            for sel in self.sel["file_input"]:
                try:
                    page.locator(sel).first.set_input_files(path, timeout=8000)
                    self.log(f"Uploaded {os.path.basename(path)}. The portal validates it in a few minutes – "
                             f"check 'Upload history' for errors, then review in GSTR-1 and file with DSC/EVC.")
                    return True
                except Exception:  # noqa: BLE001
                    continue
        self.log(f"Could not attach the file automatically. In the browser open GSTR-1 > PREPARE OFFLINE > "
                 f"Upload and choose: {path}")
        return False

    def _download_gstr1(self, y, m):
        self._dashboard(y, m)
        step = self._click("gstr1_prepare_offline", 10000) and self._click("offline_download_tab") \
            and self._click("gstr1_generate_json", 10000)
        if step:
            self.log("GSTR-1 file requested. The portal can take up to ~20 minutes to prepare it; when the "
                     "download link appears, click it in the browser – the app will capture it.")
        else:
            self.log("In the browser open GSTR-1 > PREPARE OFFLINE > Download > Generate JSON, then click the "
                     "link when ready. The app will capture it.")
        files = self._wait_new_download(90)
        return files
