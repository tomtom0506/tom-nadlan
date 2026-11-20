#!/usr/bin/env python3
"""tom-nadlan / notify_failure.py
הודעת טלגרם קצרה כש-workflow נכשל. משתמש ב-send_telegram של weekly_watch (טוקן מ-env בלבד, לא מודפס).
"""
import os
import sys

from weekly_watch import send_telegram


def build_text(title, run_url):
    text = f"⚠️ {title}"
    if run_url.startswith("https://github.com/"):
        text += f"\nפרטים: {run_url}"
    return text


def main(argv):
    title = (argv[1] if len(argv) > 1 else "workflow נכשל")[:200]
    return 0 if send_telegram(build_text(title, os.environ.get("RUN_URL", ""))) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
