#!/usr/bin/env python3
"""tom-nadlan / deals_api.py
שליפת עסקאות מה-API הציבורי של "גרסאות לעם" (/api/deals/search) וסיכום שלהן לפי חודש.

מאז 08.11.2026 נקודת ה-SQL דורשת התחברות. ה-API הזה פתוח, ומחזיר עסקה אחת לכל שורה (עד 200 בבקשה).
כל חודש נמשך פעם אחת, מסוכם, ונשמר ב-data/months/YYYY-MM.json. העדכון היומי מושך מחדש רק את
החודשים האחרונים, שאליהם עוד נוספות עסקאות באיחור, ומשלים חודשים חסרים כל עוד יש זמן.

קובץ חודש:
  month, source ("api" או "sql-legacy"), fetched_at, n (עסקאות אחרי סינון)
  days:    [[יום ISO, עיר, חדרים, n, סכום, מינ', מקס'], ...]      לגיליון היומי
  monthly: [[עיר, קבוצת חדרים, n, סכום, מינ', מקס'], ...]           לגרף "שנה" ו"3 שנים"
  budget:  {עיר: [n_all, סכום מחירים עם שטח סביר, סכום שטחים סבירים, [[מחיר, שטח], ...]]}
           לשונית ₪1.5M: כל העסקאות עד התקציב, כדי לסנן לפי ממוצע העיר על פני 12 חודשים.
בלוגים נרשם רק סוג השגיאה ותקציר בטוח של תשובת השרת. אין סודות.
"""
import http.client
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone

BASE = "https://www.over.org.il"
UA = "tom-nadlan-dashboard/1.6 (personal, read-only, daily)"
ROOT = os.path.dirname(os.path.abspath(__file__))
MONTHS_DIR = os.path.join(ROOT, "data", "months")
TIMEOUT = 60
PAUSE = 1.0
PAGE = 200                 # המקסימום שה-API מאפשר
MAX_OFFSET = 100_000
ATTEMPTS = 5
DETAIL_MAX = 300
APT_NATURES = ("דירה בבית קומות", "דירת גן", "דירת גג")
MIN_AMT, MAX_AMT = 150_000, 40_000_000
AREA_OK = (25, 400)
BUDGET = 1_500_000
MISSING_TOLERANCE = 0.002  # עד 0.2% חוסר מותר רק אם שתי דרכי השליפה מסכימות (שורות זהות לחלוטין במקור)
LEGACY_TOLERANCE = 0.03    # בדיקת אמינות: API מול ה-SQL הישן על אותו חודש

_deadline = None


class ApiError(Exception):
    pass


# ---------- זמן ----------

def start_clock(seconds):
    global _deadline
    _deadline = time.monotonic() + seconds


def time_left():
    return float("inf") if _deadline is None else _deadline - time.monotonic()


def _wait(seconds):
    if time_left() <= seconds:
        raise ApiError("deadline")
    time.sleep(seconds)


# ---------- HTTP ----------

def error_detail(e):
    """תקציר בטוח של תשובת שגיאה: כותרת WWW-Authenticate ותחילת הגוף, בלי תווי בקרה, באורך מוגבל."""
    parts = []
    auth = (e.headers or {}).get("www-authenticate") if hasattr(e, "headers") else None
    if auth:
        parts.append(f"auth={auth}")
    try:
        body = e.read(2000)
    except Exception:  # noqa: BLE001
        body = b""
    if body:
        parts.append(body.decode("utf-8", "replace"))
    text = re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]", " ", " | ".join(parts))).strip()
    return text[:DETAIL_MAX] + ("…" if len(text) > DETAIL_MAX else "")


def get_json(path, params=None):
    """GET אחד עם ניסיון חוזר על 429, 5xx, שגיאות רשת ותשובה פגומה. כל כשל יוצא כ-ApiError."""
    url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    last = "none"
    for attempt in range(1, ATTEMPTS + 1):
        if time_left() <= 0:
            raise ApiError(f"deadline (last: {last})")
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
        delay = 10 * attempt
        try:
            with urllib.request.urlopen(req, timeout=max(5, min(TIMEOUT, time_left()))) as r:
                data = json.loads(r.read(20_000_000).decode("utf-8"))
            if not isinstance(data, dict):
                raise ApiError("NotObject")
            time.sleep(PAUSE)
            return data
        except urllib.error.HTTPError as e:
            if not (e.code == 429 or e.code >= 500):
                detail = error_detail(e)
                raise ApiError(f"HTTP {e.code}" + (f": {detail}" if detail else ""))
            last = f"HTTP {e.code}"
            ra = (e.headers or {}).get("retry-after", "")
            delay = min(int(ra) + 2, 90) if str(ra).isdigit() else 15 * attempt
        except (OSError, http.client.HTTPException, ValueError) as e:
            last = type(e).__name__
        if attempt < ATTEMPTS:
            _wait(delay)
    raise ApiError(f"retries exhausted (last: {last})")


def stats():
    """טריות המאגר: עסקה ראשונה ואחרונה ומועד הסריקה."""
    return get_json("/api/deals/stats")


# ---------- שליפה ----------

def deal_key(d):
    """מפתח לזיהוי כפילויות בין דפים. אין מזהה שורה ב-API, ולכן משתמשים בכל השדות המזהים."""
    return (d.get("date"), d.get("settlement"), d.get("gush"), d.get("helka"), d.get("sub_parcel"),
            d.get("amount"), d.get("area_sqm"), d.get("rooms"), d.get("nature"), d.get("year_built"))


def fetch_range(nature, date_from, date_to, sort="amount_asc"):
    """כל העסקאות של מהות אחת בטווח תאריכים, בדפים של 200. מחזיר (רשימה בלי כפילויות, total של השרת, שורות שנקראו)."""
    seen, out, total, offset, read = set(), [], None, 0, 0
    while True:
        if offset > MAX_OFFSET:
            raise ApiError("offset limit")
        data = get_json("/api/deals/search", {"nature": nature, "date_from": date_from, "date_to": date_to,
                                              "sort": sort, "limit": PAGE, "offset": offset})
        page = data.get("data")
        if not isinstance(page, list):
            raise ApiError("NoData")
        if data.get("total_capped"):
            raise ApiError("total capped")
        total = data.get("total") if isinstance(data.get("total"), int) else total
        read += len(page)
        for d in page:
            if isinstance(d, dict):
                k = deal_key(d)
                if k not in seen:
                    seen.add(k)
                    out.append(d)
        offset += PAGE
        if len(page) < PAGE or (total is not None and offset >= total):
            return out, total, read


def _days(month):
    y, m = map(int, month.split("-"))
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    return [date(y, m, k).isoformat() for k in range(1, (nxt - date(y, m, 1)).days + 1)]


def month_bounds(month):
    days = _days(month)
    return days[0], days[-1]


def fetch_nature_month(nature, month):
    """מהות אחת בחודש אחד. אם מספר השורות הייחודיות לא תואם ל-total, שולפים שוב יום-יום.
    המיון בתוך הדף לא ייחודי, ולכן דפדוף ארוך עלול לדלג או לחזור על שורות; ביום אחד יש מעט דפים."""
    first, last = month_bounds(month)
    rows, total, _ = fetch_range(nature, first, last)
    if total is None or len(rows) == total:
        return rows
    by_day = []
    for day in _days(month):
        part, t, _ = fetch_range(nature, day, day)
        if t is not None and len(part) != t:
            part2, _, _ = fetch_range(nature, day, day, sort="area_desc")
            seen = {deal_key(d) for d in part}
            part += [d for d in part2 if deal_key(d) not in seen]
        by_day.extend(part)
    missing = total - len(by_day)
    if missing > max(1, total * MISSING_TOLERANCE) or missing < 0:
        raise ApiError(f"incomplete {nature} {month}: {len(by_day)}/{total}")
    if missing:
        print(f"months: {month} {nature}: {missing} identical rows merged ({len(by_day)}/{total})")
    return by_day


def fetch_month(month):
    deals = []
    for nature in APT_NATURES:
        deals.extend(fetch_nature_month(nature, month))
    return deals


# ---------- סיכום ----------

def _num(v):
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def rooms_bucket(r):
    if r is None or r <= 0:
        return 0
    return 2 if r < 3 else 3 if r < 4 else 4 if r < 5 else 5


def clean_deal(d):
    """עסקה תקינה לגיליון, או None. אותם כללים כמו בשאילתת ה-SQL הקודמת: דירה, עסקה על הנכס כולו, מחיר סביר."""
    if d.get("nature") not in APT_NATURES:
        return None
    frac = _num(d.get("portion_fraction"))
    if frac is None:
        frac = 1.0 if str(d.get("portion")) in ("1", "1.000") else None
    if frac != 1.0:
        return None
    amt = _num(d.get("amount"))
    day = str(d.get("date") or "")
    city = str(d.get("settlement") or "").strip()[:40]
    if amt is None or not (MIN_AMT <= amt <= MAX_AMT) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) or not city:
        return None
    rooms = _num(d.get("rooms"))
    rooms = rooms if rooms is not None and 0 < rooms <= 20 else 0
    area = _num(d.get("area_sqm"))
    return {"date": day, "city": city, "rooms": rooms, "rb": rooms_bucket(rooms), "amount": int(round(amt)),
            "area": area if area and area > 0 else 0, "gush": str(d.get("gush") or "")[:10],
            "helka": str(d.get("helka") or "")[:10], "sub": str(d.get("sub_parcel") or "")[:10],
            "year": int(_num(d.get("year_built")) or 0)}


def _agg(groups, key, amt):
    g = groups.get(key)
    if g is None:
        groups[key] = [1, amt, amt, amt]
    else:
        g[0] += 1
        g[1] += amt
        g[2] = min(g[2], amt)
        g[3] = max(g[3], amt)


def summarize(month, deals):
    """סיכום חודש מתוך עסקאות נקיות (clean_deal)."""
    days, monthly, budget = {}, {}, {}
    n = 0
    for x in deals:
        if not x["date"].startswith(month):
            continue
        n += 1
        a = x["amount"]
        _agg(days, (x["date"], x["city"], x["rooms"]), a)
        _agg(monthly, (x["city"], x["rb"]), a)
        b = budget.setdefault(x["city"], [0, 0, 0, []])
        b[0] += 1
        ok_area = AREA_OK[0] <= x["area"] <= AREA_OK[1]
        if ok_area:
            b[1] += a
            b[2] += x["area"]
        if a <= BUDGET:
            b[3].append([a, round(x["area"], 1) if x["area"] else 0])
    for b in budget.values():
        b[2] = round(b[2], 1)
        b[3].sort()
    return {
        "month": month,
        "n": n,
        "days": sorted([d, c, r, g[0], g[1], g[2], g[3]] for (d, c, r), g in days.items()),
        "monthly": sorted([c, rb, g[0], g[1], g[2], g[3]] for (c, rb), g in monthly.items()),
        "budget": {c: budget[c] for c in sorted(budget)},
    }


# ---------- אחסון ----------

def month_path(month, root=MONTHS_DIR):
    return os.path.join(root, f"{month}.json")


def load_month(month, root=MONTHS_DIR):
    try:
        with open(month_path(month, root), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) and d.get("month") == month else None
    except (OSError, ValueError):
        return None


def save_month(rec, root=MONTHS_DIR):
    os.makedirs(root, exist_ok=True)
    path = month_path(rec["month"], root)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def list_months(root=MONTHS_DIR):
    try:
        names = os.listdir(root)
    except OSError:
        return []
    return sorted(n[:7] for n in names if re.fullmatch(r"\d{4}-\d{2}\.json", n))


def check_against_previous(new, old):
    """בדיקת אמינות: אם קיים סיכום קודם (מה-SQL הישן או משליפה קודמת), ספירת העסקאות לא אמורה לרדת ביותר מ-3%.
    ירידה חדה מרמזת על שליפה חלקית, ואז לא דורסים את הקובץ הקיים."""
    if not old:
        return
    prev = old.get("n") or sum(r[2] for r in old.get("monthly") or [])
    if prev and new["n"] < prev * (1 - LEGACY_TOLERANCE):
        raise ApiError(f"check failed {new['month']}: {new['n']} < {prev} ({old.get('source')})")


def refresh_month(month, root=MONTHS_DIR, fetch=fetch_month):
    deals = [x for x in (clean_deal(d) for d in fetch(month)) if x]
    rec = summarize(month, deals)
    check_against_previous(rec, load_month(month, root))
    rec.update(source="api", fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    save_month(rec, root)
    return rec, deals


# ---------- המרה חד-פעמית מה-dashboard.json הישן ----------

def legacy_months(dash):
    """מסכם את ההיסטוריה שכבר נאספה ב-SQL (monthly ו-rows של dashboard.json הקודם) לקבצי חודש בלי budget.
    כך גרף "3 שנים" לא מתרוקן, וה-API צריך למשוך רק את החודשים האחרונים."""
    out = {}
    mo = dash.get("monthly") or {}
    months, cities = mo.get("months") or [], mo.get("cities") or []
    for r in mo.get("rows") or []:
        mi, ci, rb, n, s, mn, mx = r
        m = out.setdefault(months[mi], {"month": months[mi], "days": [], "monthly": []})
        m["monthly"].append([cities[ci], rb, n, s, mn, mx])
    days, dcities = dash.get("days") or [], dash.get("cities") or []
    for r in dash.get("rows") or []:
        di, ci, rooms, n, s, mn, mx = r
        day = days[di]
        m = out.setdefault(day[:7], {"month": day[:7], "days": [], "monthly": []})
        m["days"].append([day, dcities[ci], rooms, n, s, mn, mx])
    for month, m in out.items():
        if not m["monthly"]:  # חודשים אחרי העוגן: סיכום חודשי מתוך הימים
            agg = {}
            for day, city, rooms, n, s, mn, mx in m["days"]:
                g = agg.setdefault((city, rooms_bucket(rooms)), [0, 0, mn, mx])
                g[0] += n
                g[1] += s
                g[2] = min(g[2], mn)
                g[3] = max(g[3], mx)
            m["monthly"] = [[c, rb] + g for (c, rb), g in agg.items()]
        m["monthly"].sort()
        m["days"].sort()
        m["n"] = sum(r[2] for r in m["monthly"])
        m.update(source="sql-legacy", fetched_at=dash.get("generated_at"))
    return out
