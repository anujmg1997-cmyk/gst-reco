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
                 selectors_override=None, credentials=None):
        super().__init__(daemon=True)
        self.credentials = credentials      # callable -> (username, password) or None; read only when needed
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
                # chromium_sandbox=True: keep the browser's normal security sandbox on
                # (removes the "--no-sandbox ... security risks" warning bar)
                kw = dict(headless=False, accept_downloads=True, no_viewport=True, chromium_sandbox=True,
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
        user, pwd = (self.credentials() if self.credentials else None) or ("", "")
        if user and self._fill("login_username", user):
            filled_pwd = bool(pwd) and self._fill("login_password", pwd)
            self._focus("login_captcha")
            if filled_pwd:
                self.log("Username and password filled in. Type the CAPTCHA, click LOGIN, then enter the OTP.")
            else:
                self.log("Username filled in. Type your password and the CAPTCHA, click LOGIN, then the OTP.")
        else:
            self.log("Portal opened. Log in yourself (username, password, CAPTCHA, OTP). Tip: save the portal "
                     "username/password in Manage clients to have them filled in.")
        return True

    def _fill(self, key, value):
        page = self._active_page()
        for sel in self.sel.get(key, []):
            try:
                loc = page.locator(sel).first
                loc.wait_for(state="visible", timeout=8000)
                loc.fill(value)
                return True
            except Exception:  # noqa: BLE001
                continue
        return False

    def _focus(self, key):
        page = self._active_page()
        for sel in self.sel.get(key, []):
            try:
                page.locator(sel).first.focus(timeout=2000)
                return True
            except Exception:  # noqa: BLE001
                continue
        return False

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

    def _on_dashboard(self, page):
        return any(m in page.url for m in self.sel["dashboard_url_contains"]) and not self._denied(page)

    def _denied(self, page):
        try:
            return page.locator("text=Access Denied").first.is_visible(timeout=500)
        except Exception:  # noqa: BLE001
            return False

    def _open_returns_dashboard(self):
        """Reach the Returns Dashboard through the portal's own menu (opening its address directly is
        refused by the portal with 'Access Denied')."""
        page = self._active_page()
        if self._on_dashboard(page):
            return page
        if self._denied(page) or not any(m in page.url for m in self.sel["logged_in_url_contains"]):
            page.goto(self.sel["welcome_url"], wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            if self._denied(page) or "/services/login" in page.url:
                raise PortalError("The GST portal says the session has expired. Click 'Open portal & log in' "
                                  "and log in again.")
        # 1) a 'Return Dashboard' button/link on the welcome page, 2) Services > Returns > Returns Dashboard
        if not self._click("return_dashboard_link", 4000):
            self._hover_or_click("services_menu")
            self._hover_or_click("returns_menu")
            self._click("returns_dashboard_menu", 5000)
        end = time.time() + 20
        while time.time() < end:
            page = self._active_page()
            if self._on_dashboard(page):
                page.wait_for_timeout(1500)
                return page
            self._pump(500)
        self.log("Please open Services › Returns › Returns Dashboard in the browser yourself – the app will "
                 "continue as soon as the dashboard is open (waiting up to 3 minutes).")
        end = time.time() + 180
        while time.time() < end:
            for p in self.ctx.pages:
                if not p.is_closed() and self._on_dashboard(p):
                    self.page = p
                    p.wait_for_timeout(1500)
                    return p
            self._pump(1000)
        raise PortalError("The Returns Dashboard was not opened.")

    def _hover_or_click(self, key):
        page = self._active_page()
        for sel in self.sel.get(key, []):
            try:
                loc = page.locator(sel).first
                loc.wait_for(state="visible", timeout=3000)
                loc.hover()
                page.wait_for_timeout(400)
                loc.click()
                page.wait_for_timeout(600)
                return True
            except Exception:  # noqa: BLE001
                continue
        return False

    def _dashboard(self, y, m):
        self._ensure_login()
        page = self._open_returns_dashboard()
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
