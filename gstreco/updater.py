"""In-app updater.

The release build publishes two files on every version:
    GSTReco-Setup-<version>.exe   the installer
    latest.json                   {"version", "url", "sha256", "notes", "published"}
The app reads latest.json, and if the version is newer it downloads the installer,
checks the SHA-256, runs it silently and closes. The installer re-opens the app.
Settings and work files live outside the install folder, so they survive updates.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request

from . import UPDATE_URL, __version__


class UpdateError(Exception):
    pass


def is_installed_app() -> bool:
    return bool(getattr(sys, "frozen", False))


def parse_version(v: str):
    nums = [int(x) for x in re.findall(r"\d+", str(v))[:4]]
    return tuple(nums + [0] * (4 - len(nums)))


def _get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": f"GSTReco/{__version__}",
                                               "Accept": "application/json"})
    return urllib.request.urlopen(req, timeout=timeout)


def fetch_manifest(url: str) -> dict:
    """Reads latest.json, or a GitHub 'releases/latest' API response."""
    try:
        with _get(url) as r:
            data = json.loads(r.read().decode("utf-8-sig"))
    except Exception as e:  # noqa: BLE001
        raise UpdateError(f"Could not check for updates ({e}).")
    if "tag_name" in data:                       # GitHub API format
        exe = next((a for a in data.get("assets", []) if a["name"].lower().endswith(".exe")), None)
        if not exe:
            raise UpdateError("Latest release has no installer attached.")
        return {"version": data["tag_name"].lstrip("v"), "url": exe["browser_download_url"],
                "sha256": "", "notes": data.get("body", ""), "published": data.get("published_at", "")}
    if not data.get("version") or not data.get("url"):
        raise UpdateError("Update information is incomplete.")
    return data


def check_for_update(url: str = ""):
    """Returns the manifest if a newer version exists, else None."""
    url = url or UPDATE_URL
    if not url:
        raise UpdateError("No update address configured (Settings > Update address).")
    m = fetch_manifest(url)
    return m if parse_version(m["version"]) > parse_version(__version__) else None


def download(manifest: dict, progress=None) -> str:
    """Downloads the installer to %TEMP%; progress(done_bytes, total_bytes)."""
    url = manifest["url"]
    name = os.path.basename(url.split("?")[0]) or f"GSTReco-Setup-{manifest['version']}.exe"
    path = os.path.join(tempfile.mkdtemp(prefix="gstreco_update_"), name)
    h = hashlib.sha256()
    try:
        with _get(url, timeout=60) as r, open(path, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
    except Exception as e:  # noqa: BLE001
        raise UpdateError(f"Download failed ({e}).")
    expected = (manifest.get("sha256") or "").lower()
    if expected and h.hexdigest() != expected:
        os.remove(path)
        raise UpdateError("Downloaded file is corrupted (checksum mismatch). Please try again.")
    return path


def run_installer(path: str):
    """Starts the installer silently; the caller must then close the app."""
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([path, "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS"],
                     close_fds=True, creationflags=flags)
