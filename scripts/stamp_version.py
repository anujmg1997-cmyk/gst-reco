"""Write the release version and update address into gstreco/__init__.py.
Usage: python scripts/stamp_version.py 1.2.0 https://github.com/OWNER/REPO/releases/latest/download/latest.json"""
import os
import re
import sys

path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "gstreco", "__init__.py")
version, url = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "")
if not re.fullmatch(r"\d+\.\d+\.\d+", version):
    sys.exit(f"Version must look like 1.2.3, got {version!r}")
src = open(path, encoding="utf-8").read()
src = re.sub(r'__version__ = ".*?"', f'__version__ = "{version}"', src)
src = re.sub(r'UPDATE_URL = ".*?"', f'UPDATE_URL = "{url}"', src)
open(path, "w", encoding="utf-8").write(src)
print(f"Stamped version {version}, update URL {url or '(none)'}")
