"""Create latest.json for the in-app updater.
Usage: python scripts/make_manifest.py <installer.exe> <version> <download_url> <out.json> [notes_file]"""
import hashlib
import json
import sys
from datetime import datetime, timezone

exe, version, url, out = sys.argv[1:5]
notes = open(sys.argv[5], encoding="utf-8").read() if len(sys.argv) > 5 else ""
h = hashlib.sha256()
with open(exe, "rb") as f:
    for chunk in iter(lambda: f.read(1 << 20), b""):
        h.update(chunk)
manifest = {"version": version, "url": url, "sha256": h.hexdigest(), "notes": notes.strip(),
            "published": datetime.now(timezone.utc).strftime("%Y-%m-%d")}
with open(out, "w", encoding="utf-8") as f:
    json.dump(manifest, f, indent=2)
print(json.dumps(manifest, indent=2))
