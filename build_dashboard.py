#!/usr/bin/env python3
"""tom-nadlan / build_dashboard.py
בונה את data/dashboard.json לגיליון הראשי: דירות שנמכרו, לפי יום, עיר ומספר חדרים.
המקור: נקודת הקצה של שאילתות SQL ב"גרסאות לעם" (רשות המסים). קריאה בלבד.

- רק דירות (דירה בבית קומות, דירת גן, דירת גג), עסקה על הדירה כולה (portion=1), מחיר סביר.
- המאגר מתעדכן בפיגור, ולכן "העוגן" הוא היום האחרון בחודש המלא האחרון, לא התאריך של היום.
- בלוגים נרשם רק סוג השגיאה.
"""
import json
import os
import re
import statistics
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timezone

BUILD_VERSION = "1.4.0"
DATASET_ID = "fd06f5ae-8a4f-4120-b275-8a514ad23499"
TABLE = "append_taxes_nadlan_full_f41fb496_fd06f5ae"
SQL_URL = f"https://www.over.org.il/api/append/{DATASET_ID}/sql"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "dashboard.json")
UA = "tom-nadlan-dashboard/1.4 (personal, read-only, daily)"
TIMEOUT = 60
PAUSE = 1.5
PAGE = 500
MAX_PAGES = 80
MONTHS_BACK = 3          # חודש העוגן ועוד שניים לפניו (כ-90 יום)
FULL_MONTH_RATIO = 0.70  # חודש "מלא" = לפחות 70% מהחציון של 6 החודשים שלפניו

APT_NATURES = ("דירה בבית קומות", "דירת גן", "דירת גג")
NATURE_SQL = "deal_nature IN (" + ",".join("'" + n.replace("'", "''") + "'" for n in APT_NATURES) + ")"
AMT = "CAST(deal_amount AS REAL)"
BASE_WHERE = f"{NATURE_SQL} AND (portion = '1.000' OR portion = '1') AND {AMT} BETWEEN 150000 AND 40000000"


class SqlError(Exception):
    pass


def post_sql(sql):
    """שאילתה אחת, עם ניסיון חוזר על 429 ו-5xx. מחזיר רשימת שורות."""
    body = json.dumps({"sql": sql}).encode("utf-8")
    for attempt in range(1, 6):
        req = urllib.request.Request(SQL_URL, data=body, method="POST", headers={
            "User-Agent": UA, "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                data = json.loads(r.read(20_000_000).decode("utf-8"))
                rows = data.get("rows") if isinstance(data, dict) else None
                if not isinstance(rows, list):
                    raise SqlError("NoRows")
                time.sleep(PAUSE)
                return rows
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                ra = (e.headers or {}).get("retry-after", "")
                time.sleep(min(int(ra) + 2, 90) if str(ra).isdigit() else 15 * attempt)
                continue
            raise SqlError(f"HTTP {e.code}")
        except (urllib.error.URLError, TimeoutError) as e:
            time.sleep(10 * attempt)
            last = type(e).__name__
            continue
    raise SqlError("retries exhausted")


def month_counts():
    years = [date.today().year - 1, date.today().year]
    yfilter = " OR ".join(f"deal_date LIKE '%/{y}'" for y in years)
    sql = (f"SELECT substr(deal_date, 4, 7) AS ym, COUNT(*) AS n FROM {TABLE} "
           f"WHERE ({yfilter}) AND {BASE_WHERE} GROUP BY substr(deal_date, 4, 7)")
    out = {}
    for r in post_sql(sql):
        m = re.fullmatch(r"(\d{2})/(\d{4})", str(r.get("ym", "")))
        if m:
            out[f"{m.group(2)}-{m.group(1)}"] = int(r.get("n") or 0)
    return out


def pick_anchor_month(counts, today=None):
    """החודש המלא האחרון: לא החודש הנוכחי, ועם לפחות FULL_MONTH_RATIO מהחציון של 6 החודשים שלפניו."""
    today = today or date.today()
    cur = f"{today.year}-{today.month:02d}"
    months = sorted(m for m in counts if m < cur and counts[m] > 0)
    for i in range(len(months) - 1, -1, -1):
        prev = [counts[m] for m in months[max(0, i - 6):i]]
        if not prev:
            return months[i]
        if counts[months[i]] >= FULL_MONTH_RATIO * statistics.median(prev):
            return months[i]
    return None


def months_window(anchor, back=MONTHS_BACK, extra_after=None):
    y, m = map(int, anchor.split("-"))
    res = []
    for k in range(back - 1, -1, -1):
        yy, mm = y, m - k
        while mm <= 0:
            mm += 12
            yy -= 1
        res.append(f"{yy}-{mm:02d}")
    return res + sorted(extra_after or [])


def fetch_detail(months):
    like = " OR ".join(f"deal_date LIKE '%/{m[5:7]}/{m[0:4]}'" for m in months)
    rows, offset = [], 0
    for _ in range(MAX_PAGES):
        sql = (f"SELECT deal_date, settlement, room_num, COUNT(*) AS n, SUM({AMT}) AS s, "
               f"MIN({AMT}) AS mn, MAX({AMT}) AS mx FROM {TABLE} WHERE ({like}) AND {BASE_WHERE} "
               f"GROUP BY deal_date, settlement, room_num ORDER BY deal_date, settlement, room_num "
               f"LIMIT {PAGE} OFFSET {offset}")
        page = post_sql(sql)
        rows.extend(page)
        if len(page) < PAGE:
            return rows
        offset += PAGE
    raise SqlError("too many pages")


BUDGET = 1500000
AREA = "CAST(NULLIF(asset_area, '') AS REAL)"


def fetch_budget(anchor_month):
    """12 חודשים עד חודש העוגן: לכל עיר, כמה דירות נמכרו עד התקציב, ובאיזה גודל ומחיר."""
    months = months_window(anchor_month, back=12)
    like = " OR ".join(f"deal_date LIKE '%/{m[5:7]}/{m[0:4]}'" for m in months)
    inb = f"{AMT} <= {BUDGET}"
    ok_area = f"{AREA} BETWEEN 25 AND 400"
    sql = (f"SELECT settlement, COUNT(*) AS n_all, "
           f"SUM(CASE WHEN {inb} THEN 1 ELSE 0 END) AS n_b, "
           f"SUM(CASE WHEN {inb} THEN {AMT} ELSE 0 END) AS s_b, "
           f"SUM(CASE WHEN {inb} AND {ok_area} THEN 1 ELSE 0 END) AS na_b, "
           f"SUM(CASE WHEN {inb} AND {ok_area} THEN {AREA} ELSE 0 END) AS a_b, "
           f"SUM(CASE WHEN {inb} AND {ok_area} THEN {AMT} ELSE 0 END) AS sa_b "
           f"FROM {TABLE} WHERE ({like}) AND {BASE_WHERE} GROUP BY settlement LIMIT 3000")
    rows = []
    for r in post_sql(sql):
        name = str(r.get("settlement") or "").strip()[:40]
        vals = [num(r.get(k)) for k in ("n_all", "n_b", "s_b", "na_b", "a_b", "sa_b")]
        if not name or any(v is None for v in vals) or vals[0] <= 0:
            continue
        rows.append([name] + [round(v) for v in vals])
    rows.sort(key=lambda x: x[0])
    return {"from": months[0], "to": months[-1], "limit": BUDGET, "rows": rows}


PAGE_M = 1000
MAX_PAGES_M = 150
ROOMS = "CAST(NULLIF(room_num, '') AS REAL)"
RB = (f"CASE WHEN {ROOMS} IS NULL OR {ROOMS} <= 0 THEN 0 WHEN {ROOMS} < 3 THEN 2 "
      f"WHEN {ROOMS} < 4 THEN 3 WHEN {ROOMS} < 5 THEN 4 ELSE 5 END")


def fetch_monthly(anchor_month):
    """36 חודשים עד חודש העוגן: ספירה, סכום, מינ' ומקס' לפי חודש, עיר וקבוצת חדרים (0 = לא ידוע, 2 = עד 2.5, 3, 4, 5+)."""
    months = months_window(anchor_month, back=36)
    years = sorted({m[:4] for m in months})
    yfilter = " OR ".join(f"deal_date LIKE '%/{y}'" for y in years)
    ym = "substr(deal_date, 4, 7)"
    raw, offset = [], 0
    for _ in range(MAX_PAGES_M):
        sql = (f"SELECT {ym} AS ym, settlement, {RB} AS rb, COUNT(*) AS n, SUM({AMT}) AS s, MIN({AMT}) AS mn, MAX({AMT}) AS mx FROM {TABLE} "
               f"WHERE ({yfilter}) AND {BASE_WHERE} GROUP BY {ym}, settlement, {RB} "
               f"ORDER BY 1, 2, 3 LIMIT {PAGE_M} OFFSET {offset}")
        page = post_sql(sql)
        raw.extend(page)
        if len(page) < PAGE_M:
            break
        offset += PAGE_M
    else:
        raise SqlError("too many pages (monthly)")
    mi = {m: i for i, m in enumerate(months)}
    cities, rows = {}, []
    for r in raw:
        mm = re.fullmatch(r"(\d{2})/(\d{4})", str(r.get("ym", "")))
        if not mm:
            continue
        key = f"{mm.group(2)}-{mm.group(1)}"
        name = str(r.get("settlement") or "").strip()[:40]
        n, sm, rb = num(r.get("n")), num(r.get("s")), num(r.get("rb"))
        mn, mx = num(r.get("mn")), num(r.get("mx"))
        if key not in mi or not name or not n or n <= 0 or sm is None or rb not in (0, 2, 3, 4, 5):
            continue
        if mn is None or mx is None or mx < mn:
            continue
        ci = cities.setdefault(name, len(cities))
        rows.append([mi[key], ci, int(rb), int(n), round(sm), round(mn), round(mx)])
    names = [None] * len(cities)
    for k, v in cities.items():
        names[v] = k
    return {"months": months, "cities": names, "rows": rows}


def freshness():
    try:
        r = post_sql(f"SELECT MAX(scraped_at) AS scraped, MAX(first_seen) AS seen FROM {TABLE}")
        return (r[0] or {}) if r else {}
    except SqlError:
        return {}


def to_iso(d):
    m = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", str(d or ""))
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else None


def num(v):
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def pack(raw_rows, anchor_month):
    """דחיסה: רשימת ימים, רשימת ערים, ושורות [יום, עיר, חדרים, n, סכום, מינ', מקס']."""
    days, cities, out = {}, {}, []
    for r in raw_rows:
        d = to_iso(r.get("deal_date"))
        city = str(r.get("settlement") or "").strip()[:40]
        n = int(num(r.get("n")) or 0)
        s, mn, mx = num(r.get("s")), num(r.get("mn")), num(r.get("mx"))
        rooms = num(r.get("room_num")) or 0
        if not d or not city or n <= 0 or s is None or mn is None or mx is None:
            continue
        if not (0 <= rooms <= 20):
            rooms = 0
        days.setdefault(d, None)
        cities.setdefault(city, None)
        out.append([d, city, rooms, n, round(s), round(mn), round(mx)])
    day_list = sorted(days)
    city_list = sorted(cities)
    di = {d: i for i, d in enumerate(day_list)}
    ci = {c: i for i, c in enumerate(city_list)}
    packed = [[di[r[0]], ci[r[1]], r[2], r[3], r[4], r[5], r[6]] for r in out]
    anchor_days = [d for d in day_list if d.startswith(anchor_month)]
    anchor = anchor_days[-1] if anchor_days else None
    return {"days": day_list, "cities": city_list, "rows": packed, "anchor": anchor}


def build(today=None):
    counts = month_counts()
    anchor_month = pick_anchor_month(counts, today)
    if not anchor_month:
        raise SqlError("no full month")
    later = [m for m in counts if m > anchor_month and counts[m] > 0]
    months = months_window(anchor_month, extra_after=later)
    detail = fetch_detail(months)
    data = pack(detail, anchor_month)
    try:
        budget = fetch_budget(anchor_month)
    except SqlError as e:
        print(f"budget: failed ({e})")
        budget = None
    try:
        monthly = fetch_monthly(anchor_month)
    except SqlError as e:
        print(f"monthly: failed ({e})")
        monthly = None
    fr = freshness()
    return {
        "build_version": BUILD_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "רשות המסים, דרך גרסאות לעם (over.org.il)",
        "natures": list(APT_NATURES),
        "anchor_month": anchor_month,
        "anchor": data["anchor"],
        "months": [{"ym": m, "n": counts[m]} for m in sorted(counts)][-14:],
        "db_scraped_at": str(fr.get("scraped") or "")[:19] or None,
        "db_first_seen": str(fr.get("seen") or "")[:19] or None,
        "days": data["days"],
        "cities": data["cities"],
        "rows": data["rows"],
        "budget": budget,
        "monthly": monthly,
    }


def main():
    report = build()
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, OUT)
    print(f"dashboard: anchor={report['anchor']} days={len(report['days'])} "
          f"cities={len(report['cities'])} rows={len(report['rows'])}")


if __name__ == "__main__":
    try:
        main()
    except SqlError as e:
        print(f"dashboard: failed ({e})")
        raise SystemExit(1)
