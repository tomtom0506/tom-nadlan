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

WATCH_VERSION = "1.5.0"
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


def city_block(dash, wc):
    name = wc["name"]
    lines = [f"🏙 {name}"]
    cm = city_monthly(dash, name)
    if cm:
        months, n, s = cm
        last = len(months) - 1
        ym = months[last]
        prev_y = str(int(ym[:4]) - 1)
        pv = [n[i] for i, m in enumerate(months) if m.startswith(prev_y)]
        line = f"{month_name(ym)}: {n[last]:,} עסקאות"
        if len(pv) >= 6 and sum(pv) > 0:
            d = n[last] / (sum(pv) / len(pv)) - 1
            line += f" ({'+' if d > 0 else ''}{round(d * 100)}% מול ממוצע {prev_y})"
        lines.append(line)
        if n[last]:
            lines.append(f"ממוצע {nis(s[last] / n[last])}")
    if wc.get("median_ppm"):
        lines.append(f"חציון 3 חודשים: {nis(wc['median_ppm'])} למ\"ר")
    flags = wc.get("flags") or []
    if flags:
        tot = wc.get("flags_total", len(flags))
        lines.append(f"💎 {tot} עסקאות לפחות {dash['watch'].get('below_pct', 15)}% מתחת לחציון:" if tot > 1
                     else f"💎 עסקה אחת לפחות {dash['watch'].get('below_pct', 15)}% מתחת לחציון:")
        if tot > len(flags):
            lines.append(f"(מוצגות {len(flags)} הזולות ביותר)")
        for f in flags:
            dd = str(f.get("date", ""))
            date_s = f"{dd[8:10]}.{dd[5:7]}" if len(dd) == 10 else ""
            rooms = f.get("rooms")
            rooms_s = (f"{rooms:g} חד׳, " if isinstance(rooms, (int, float)) else "")
            parcel = f" גוש {f['gush']} חלקה {f['chelka']}" if f.get("gush") and f.get("chelka") else ""
            lines.append(f"• {date_s} {rooms_s}{f.get('area')} מ\"ר, {nis(f.get('amount', 0))} "
                         f"({nis(f.get('ppm', 0))} למ\"ר, {f.get('pct')}%){parcel}")
    return "\n".join(lines)


def build_message(dash):
    w = dash.get("watch") or {}
    cities = w.get("cities") or []
    if not cities:
        return None
    head = [f"📈 הנדלן של תומר: מעקב שבועי",
            f"עסקאות מעודכנות עד {month_name(dash['anchor_month'])}"]
    blocks = [city_block(dash, c) for c in cities]
    msg = "\n".join(head) + "\n\n" + "\n\n".join(blocks) + "\n\n" + APP_URL
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
