#!/usr/bin/env python3
"""tom-nadlan / notify_bargains.py
התראת טלגרם על מה שחדש בלשונית "מציאות": מכרזי רמ"י פתוחים שלא נראו קודם, ועסקאות GovMap חדשות
שנסגרו הרבה מתחת לעסקאות דומות. שולח רק כשיש משהו חדש. המצב (מזהים שכבר נשלחו) נשמר
ב-data/bargains_state.json. טוקנים מגיעים מ-env בלבד ולא מודפסים.
"""
import html
import json
import os
import sys

from weekly_watch import LRI, PDI, nis, send_telegram

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data", "bargains.json")
STATE = os.path.join(ROOT, "data", "bargains_state.json")
APP_URL = "https://tomtom0506.github.io/tom-nadlan/#deals"
MAX_TENDERS, MAX_FLAGS, KEEP_IDS, MAX_LEN = 8, 8, 3000, 3900
KIND_LBL = {"self": "🏡 מגרש לבנייה עצמית", "lottery": "🎟️ הרשמה והגרלה", "target": "🏷️ מחיר מטרה", "dev": "🏗️ מכרז ליזמים"}


def iso(t):
    return f"{LRI}{t}{PDI}"


def esc(s):
    return html.escape(str(s or ""), quote=False)


def ddmm(d):
    d = str(d or "")
    return f"{d[8:10]}.{d[5:7]}" if len(d) >= 10 else ""


def tender_line(t):
    parts = [f"{KIND_LBL.get(t.get('kind'), '')} · <b>{esc(t.get('city'))}</b>"]
    info = []
    if t.get("units"):
        info.append(f"{iso(t['units'])} יח\"ד")
    if t.get("appraisal_per_unit"):
        info.append(f"שומה {iso(nis(t['appraisal_per_unit']))} ליחידה")
    h = t.get("history")
    if h and h.get("per_unit"):
        info.append(f"זכיות קודמות {iso(nis(h['per_unit']))}")
    if info:
        parts.append(" · ".join(info))
    parts.append(f"הגשה עד {iso(ddmm(t.get('deadline')))}")
    line = "\n".join(parts)
    if t.get("url"):
        line += f'\n<a href="{html.escape(t["url"], quote=True)}">למכרז</a>'
    return line


def flag_line(city, f):
    where = " ".join(x for x in (f.get("street"), f.get("house")) if x)
    return (f"💎 <b>{esc(city)}</b>{' · ' + esc(where) if where else ''}\n"
            f"{iso(f.get('rooms'))} חד׳, {iso(f.get('area'))} מ\"ר · {iso(nis(f.get('amount')))} · "
            f"{iso(str(f.get('pct')) + '%')} מתחת לדומות · {iso(ddmm(f.get('date')))}")


def collect_new(data, state):
    seen_t, seen_f = set(state.get("tenders", [])), set(state.get("flags", []))
    tenders = [t for t in data.get("tenders") or [] if not t.get("stale") and t.get("kind") != "dev" and t.get("id") not in seen_t]
    flags = []
    for c in data.get("cities") or []:
        if c.get("stale"):
            continue
        for f in c.get("flags") or []:
            if f.get("id") not in seen_f:
                flags.append((c.get("name"), f))
    flags.sort(key=lambda cf: cf[1].get("pct", 0))
    return tenders, flags


def build_message(tenders, flags, first=False):
    if not tenders and not flags:
        return None
    lines = ["💎 <b>מציאות נדל\"ן</b>" + (" · התחלת מעקב" if first else " · חדש")]
    if tenders:
        lines += ["", f"<b>מכרזי רמ\"י פתוחים</b> ({iso(len(tenders))})"]
        lines += [tender_line(t) for t in tenders[:MAX_TENDERS]]
        if len(tenders) > MAX_TENDERS:
            lines.append(f"ועוד {iso(len(tenders) - MAX_TENDERS)} באפליקציה")
    if flags:
        lines += ["", f"<b>עסקאות שנסגרו מתחת לשוק</b> ({iso(len(flags))})"]
        lines += [flag_line(c, f) for c, f in flags[:MAX_FLAGS]]
        if len(flags) > MAX_FLAGS:
            lines.append(f"ועוד {iso(len(flags) - MAX_FLAGS)} באפליקציה")
        lines.append("<i>עסקאות שכבר נסגרו: הן מראות איפה ובאיזה מחיר אפשר לסגור, ולא דירות שאפשר לקנות.</i>")
    lines += ["", APP_URL]
    out = []
    for ln in lines:  # חיתוך לפי שורות שלמות, כדי לא לשבור תגית HTML באמצע
        if len("\n".join(out + [ln])) > MAX_LEN:
            break
        out.append(ln)
    return "\n".join(out)


def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def main():
    data = load_json(DATA)
    if not data:
        print("notify_bargains: no data")
        return 0
    state = load_json(STATE)
    first = state is None
    state = state or {}
    tenders, flags = collect_new(data, state)
    text = build_message(tenders, flags, first)
    if text:
        if not send_telegram(text, html_mode=True):
            print("notify_bargains: send failed")
            return 1
        print(f"notify_bargains: sent {len(tenders)} tenders, {len(flags)} flags")
    else:
        print("notify_bargains: nothing new")
    state["tenders"] = (state.get("tenders", []) + [t["id"] for t in tenders])[-KEEP_IDS:]
    state["flags"] = (state.get("flags", []) + [f["id"] for _, f in flags])[-KEEP_IDS:]
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, separators=(",", ":"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
