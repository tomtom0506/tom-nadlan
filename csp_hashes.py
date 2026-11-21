#!/usr/bin/env python3
"""tom-nadlan / csp_hashes.py
מחשב את ה-sha256 של כל <script> מוטמע ב-index.html ומעדכן את script-src ב-CSP.
הרצה אחרי כל שינוי בסקריפט: python3 csp_hashes.py --write. בלי --write: בדיקה בלבד (קוד יציאה 1 אם לא תואם).
"""
import base64
import hashlib
import os
import re
import sys

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
CSP_RE = re.compile(r"(script-src )((?:'sha256-[A-Za-z0-9+/=]+' ?)+)(;)")


def inline_hashes(html):
    bodies = re.findall(r"<script>(.*?)</script>", html, flags=re.S)
    return ["sha256-" + base64.b64encode(hashlib.sha256(b.encode("utf-8")).digest()).decode() for b in bodies]


def current_hashes(html):
    m = CSP_RE.search(html)
    return re.findall(r"'(sha256-[^']+)'", m.group(2)) if m else []


def main(argv):
    html = open(PATH, encoding="utf-8").read()
    want, have = inline_hashes(html), current_hashes(html)
    if want == have:
        print(f"csp: ok ({len(want)} scripts)")
        return 0
    if "--write" not in argv:
        print(f"csp: mismatch ({len(want)} scripts, {len(have)} hashes). run: python3 csp_hashes.py --write")
        return 1
    new = CSP_RE.sub(lambda m: m.group(1) + " ".join(f"'{h}'" for h in want) + m.group(3), html, count=1)
    with open(PATH, "w", encoding="utf-8") as f:
        f.write(new)
    print(f"csp: updated ({len(want)} scripts)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
