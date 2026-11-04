#!/usr/bin/env python3
"""tom-nadlan / weekly_watch.py
התראה שבועית בטלגרם על ערי המעקב. קורא את data/dashboard.json שנבנה בעדכון היומי.
שולח רק כשיש נתונים חדשים: חודש עוגן חדש או עדכון של המאגר מאז ההתראה הקודמת.
המצב נשמר ב-data/watch_state.json. טוקנים לא מודפסים לעולם.
"""
import json
import os
import re
import sys
import urllib.parse
import urllib.request

WATCH_VERSION = "1.5.3"
ROOT = os.path.dirname(os.path.abspath(__file__))
DASH = os.path.join(ROOT, "data", "dashboard.json")
STATE = os.path.join(ROOT, "data", "watch_state.json")
APP_URL = "https://tomtom0506.github.io/tom-nadlan/#track"
HEB_M = ["ינואר", "פברואר", "מרץ", "אפריל", "מאי", "יוני", "יולי", "אוגוסט", "ספטמבר", "אוקטובר", "נובמבר", "דצמבר"]
RB_LBL = {2: "עד 2.5 חד׳", 3: "3–3.5 חד׳", 4: "4–4.5 חד׳", 5: "5+ חד׳"}
MAX_LEN = 3900


def nis(v):
    v = float(v)
    if v >= 1e6:
        return "₪" + f"{v / 1e6:.2f}".rstrip("0").rstrip(".") + "M"
    return "₪" + f"{round(v):,}"


def month_name(ym):
    return f"{HEB_M[int(ym[5:7]) - 1]} {ym[:4]}"


def city_monthly(dash, city):
    m = dash.get("monthly") or {}
    months, cities, rows = m.get("months") or [], m.get("cities") or [], m.get("rows") or []
    if city not in cities or not months:
        return None
    ci = cities.index(city)
    n = [0] * len(months)
    s = [0] * len(months)
    for r in rows:
        if isinstance(r, list) and len(r) >= 5 and r[1] == ci and 0 <= r[0] < len(months):
            n[r[0]] += r[3]
            s[r[0]] += r[4]
    return months, n, s


LRI, PDI = "\u2066", "\u2069"  # בידוד כיווניות: כל מספר נשאר במקומו בשורה עברית


def iso(t):
    return f"{LRI}{t}{PDI}"
TOP_FLAGS = 3


def short_name(name):
    return re.sub(r"\s*-\s*", "-", name).strip()


def k(v):
    return iso(f"₪{v / 1000:.1f}K")


def pct_s(p):
    return iso(f"{'+' if p > 0 else ''}{p}%")


def city_line(dash, wc):
    parts = []
    cm = city_monthly(dash, wc["name"])
    if cm:
        months, n, _ = cm
        ym = months[-1]
        prev_y = str(int(ym[:4]) - 1)
        pv = [n[i] for i, m in enumerate(months) if m.startswith(prev_y)]
        parts.append(iso(f"{n[-1]:,}"))
        if len(pv) >= 6 and sum(pv) > 0:
            parts.append(pct_s(round((n[-1] / (sum(pv) / len(pv)) - 1) * 100)))
    if wc.get("median_ppm"):
        parts.append(k(wc["median_ppm"]))
    return f"{short_name(wc['name'])}: " + (", ".join(parts) if parts else "אין נתונים")


def top_flags(cities):
    best = []
    for c in cities:
        fl = [f for f in (c.get("flags") or []) if isinstance(f.get("pct"), (int, float))]
        if fl:
            f = min(fl, key=lambda x: x["pct"])
            best.append((f["pct"], c["name"], f))
    best.sort(key=lambda x: x[0])
    out = []
    for p, name, f in best[:TOP_FLAGS]:
        rooms = f.get("rooms")
        rooms_s = f"{iso(f'{rooms:g}')} חד׳, " if isinstance(rooms, (int, float)) else ""
        out.append(f"• {short_name(name)}: {rooms_s}{iso(f.get('area'))} מ\"ר, {iso(nis(f.get('amount', 0)))}, {pct_s(p)}")
    return out


def build_message(dash):
    w = dash.get("watch") or {}
    cities = w.get("cities") or []
    if not cities:
        return None
    m = dash.get("monthly") or {}
    ym = (m.get("months") or [dash["anchor_month"]])[-1]
    prev_y = str(int(ym[:4]) - 1)
    lines = [f"📈 הנדלן של תומר: {month_name(ym)}",
             f"(עסקאות בחודש, שינוי מול הממוצע של {prev_y}, חציון למ\"ר)", ""]
    lines += [city_line(dash, c) for c in cities]
    flags = top_flags(cities)
    if flags:
        lines += ["", "💎 הזדמנויות בולטות:"] + flags
    lines += ["", "לפרטים, גוש וחלקה:", APP_URL]
    msg = "\n".join(lines)
    if len(msg) > MAX_LEN:
        msg = msg[:MAX_LEN - len(APP_URL) - 5] + "…\n" + APP_URL
    return msg


def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def data_key(dash):
    return f"{dash.get('anchor_month')}|{dash.get('db_scraped_at')}"


def send_telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat or not re.fullmatch(r"-?\d{3,20}", chat):
        print("telegram: skipped (secrets)")
        return False
    data = urllib.parse.urlencode({"chat_id": chat, "text": text, "disable_web_page_preview": "true"}).encode()
    try:
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data, method="POST")
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status == 200
    except Exception as e:  # noqa: BLE001
        print(f"telegram: failed ({type(e).__name__})")
        return False


def main(force=False):
    dash = load_json(DASH)
    if not dash or not dash.get("watch") or not dash.get("anchor_month"):
        print("watch: no data")
        return 0
    state = load_json(STATE) or {}
    key = data_key(dash)
    if not force and state.get("last_key") == key:
        print("watch: no new data, skipped")
        return 0
    msg = build_message(dash)
    if not msg:
        print("watch: nothing to send")
        return 0
    if send_telegram(msg):
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump({"last_key": key, "sent_at": dash.get("generated_at"), "version": WATCH_VERSION}, f, ensure_ascii=False)
        print("watch: sent")
    return 0


if __name__ == "__main__":
    sys.exit(main(force=os.environ.get("FORCE_SEND") == "1"))
