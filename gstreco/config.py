"""Settings and client (company) profiles.

Settings live in %APPDATA%\\GSTReco\\settings.json (folder name kept from v1.0 so upgrades keep data).
Each client has its own work folder:  <work_dir>\\<Client name> (<GSTIN>)\\<period>\\...
and its own browser profile, so GST-portal logins of different clients never mix.
"""
import json
import os
import re
import uuid

APP_NAME = "GST ReconX"
COMPANY = "Total Strategic Solutions Pvt. Ltd."

APP_DIR = os.environ.get("GSTRECONX_HOME") or os.path.join(
    os.environ.get("APPDATA") or os.path.expanduser("~"), "GSTReco")
SETTINGS_PATH = os.path.join(APP_DIR, "settings.json")

DEFAULTS = {
    "tally_host": "localhost",
    "tally_port": 9000,
    "tolerance": 1.0,
    "work_dir": os.path.join(os.path.expanduser("~"), "Documents", "GST ReconX"),
    "browser": "msedge",
    "update_url_override": "",
    "clients": [],
    "current_client": "",
}


def load():
    s = dict(DEFAULTS)
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            s.update(json.load(f))
    except (OSError, ValueError):
        pass
    s["clients"] = [c for c in s.get("clients") or [] if isinstance(c, dict) and c.get("id")]
    # migrate v1.0 single-company settings into a first client
    if not s["clients"] and (s.get("gstin") or s.get("tally_company")):
        s["clients"].append(new_client(s.get("tally_company") or "My company", s.get("gstin", ""),
                                       s.get("tally_company", "")))
    if s["clients"] and s.get("current_client") not in {c["id"] for c in s["clients"]}:
        s["current_client"] = s["clients"][0]["id"]
    return s


def save(s):
    os.makedirs(APP_DIR, exist_ok=True)
    tmp = SETTINGS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2)
    os.replace(tmp, SETTINGS_PATH)


def new_client(name, gstin="", tally_company=""):
    return {"id": uuid.uuid4().hex[:12], "name": name.strip(), "gstin": gstin.strip().upper(),
            "tally_company": tally_company.strip()}


def _safe(name):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .") or "Client"


def client_dir(settings, client):
    label = _safe(client.get("name") or "Client")
    if client.get("gstin"):
        label += f" ({client['gstin']})"
    d = os.path.join(settings["work_dir"], label)
    os.makedirs(d, exist_ok=True)
    return d


def portal_files_dir(settings, client):
    d = os.path.join(client_dir(settings, client), "Portal files")
    os.makedirs(d, exist_ok=True)
    return d


def profile_dir(client=None):
    key = (client or {}).get("gstin") or (client or {}).get("id") or "default"
    return os.path.join(APP_DIR, "browser_profiles", _safe(key))


def selectors_override():
    return os.path.join(APP_DIR, "portal_selectors.json")
