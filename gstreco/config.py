import json
import os

APP_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "GSTReco")
SETTINGS_PATH = os.path.join(APP_DIR, "settings.json")

DEFAULTS = {
    "gstin": "",
    "tally_host": "localhost",
    "tally_port": 9000,
    "tally_company": "",
    "tolerance": 1.0,
    "work_dir": os.path.join(os.path.expanduser("~"), "Documents", "GSTReco"),
    "browser": "msedge",
}


def load():
    s = dict(DEFAULTS)
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            s.update(json.load(f))
    except (OSError, ValueError):
        pass
    return s


def save(s):
    os.makedirs(APP_DIR, exist_ok=True)
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2)


def profile_dir():
    return os.path.join(APP_DIR, "browser_profile")


def selectors_override():
    return os.path.join(APP_DIR, "portal_selectors.json")
