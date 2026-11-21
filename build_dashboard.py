#!/usr/bin/env python3
"""tom-nadlan / build_dashboard.py
בונה את data/dashboard.json לגיליון הראשי: דירות שנמכרו, לפי יום, עיר ומספר חדרים.

המקור: ה-API הציבורי של "גרסאות לעם" (/api/deals/*, נתוני רשות המסים). קריאה בלבד.
עד 07.11.2026 הנתונים נשלפו בשאילתות SQL; מאז נקודת ה-SQL דורשת התחברות (401).
השליפה והסיכום החודשי נמצאים ב-deals_api.py. כאן:
  1. מושכים מחדש את החודשים האחרונים ומשלימים חודשים חסרים, כל עוד יש זמן.
  2. בונים את dashboard.json מתוך data/months/, באותו פורמט שהאפליקציה מכירה.

- רק דירות (דירה בבית קומות, דירת גן, דירת גג), עסקה על הדירה כולה, מחיר סביר.
- המאגר מתעדכן בפיגור, ולכן "העוגן" הוא היום האחרון בחודש המלא האחרון, לא התאריך של היום.
- בלוגים נרשם רק סוג השגיאה.
"""
import json
import os
import re
import statistics
from datetime import date, datetime, timezone

import deals_api as api
from deals_api import ApiError as SqlError  # השם הישן נשאר, כדי שקוד ובדיקות קיימים ימשיכו לעבוד

BUILD_VERSION = "1.6.1"
ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "data", "dashboard.json")
MONTHS_BACK = 3           # חודש העוגן ועוד שניים לפניו (כ-90 יום)
FULL_MONTH_RATIO = 0.70   # חודש "מלא" = לפחות 70% מהחציון של 6 החודשים שלפניו
BUDGET_SECONDS = 40 * 60  # תקציב זמן לכל הבנייה. ה-job מוגבל ל-45 דקות, וצריך זמן גם ל-commit
RECENT_MONTHS = 4         # נמשכים מחדש בכל ריצה: עסקאות מגיעות באיחור של כמה חודשים
KEEP_MONTHS = 51          # כל החלון של הגרף (48 חודשים) ועוד פיגור העוגן: הכל מה-API, כי ב-SQL הישן היו כפילויות
MONTH_RESERVE = 150       # שניות: לא מתחילים חודש חדש אם נשאר פחות מזה
MONTHLY_BACK = 48         # 36 לגרף ועוד 12, כדי שלכל חודש בגרף תהיה שנה קודמת להשוואה
BUDGET = api.BUDGET
PPM_FLOOR = 0.35          # עסקה שמחירה למ"ר נמוך מ-35% מהממוצע של העיר: כנראה לא עסקת שוק או שגיאת שטח
APT_NATURES = api.APT_NATURES
rooms_bucket = api.rooms_bucket


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


def num(v):
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


# ---------- שליפה ----------

def plan_months(last_month, have, extra=()):
    """איזה חודשים למשוך, לפי סדר: קודם החודשים האחרונים וחודשי המעקב (extra), מהחדש לישן,
    ואחר כך חודשים חסרים בחלון של KEEP_MONTHS. have: חודש -> מקור הקובץ הקיים ("api" או "sql-legacy")."""
    recent = sorted(set(months_window(last_month, RECENT_MONTHS)) | set(extra), reverse=True)
    missing = [m for m in reversed(months_window(last_month, KEEP_MONTHS)) if m not in recent and have.get(m) != "api"]
    return recent + missing


def migrate_legacy(prev, root=None):
    """פעם אחת: ההיסטוריה שנאספה ב-SQL עוברת לקבצי חודש, כדי שגרף "3 שנים" לא יתרוקן."""
    root = root or api.MONTHS_DIR
    if api.list_months(root) or not prev:
        return 0
    recs = api.legacy_months(prev)
    for rec in recs.values():
        api.save_month(rec, root)
    print(f"months: migrated {len(recs)} legacy months")
    return len(recs)


def refresh(last_month, root=None, fetch=None, extra=()):
    """מושך חודשים לפי plan_months כל עוד יש זמן. מחזיר (עסקאות לפי חודש מהריצה הזו, שגיאות, חודשים שלא הספיקו)."""
    root = root or api.MONTHS_DIR
    have = {m: (api.load_month(m, root) or {}).get("source") for m in api.list_months(root)}
    deals, errors, todo = {}, [], plan_months(last_month, have, extra)
    for i, month in enumerate(todo):
        if api.time_left() < MONTH_RESERVE:
            left = [m for m in todo[i:] if have.get(m) != "api"]
            if left:
                print(f"months: out of time, {len(left)} months still missing: {', '.join(left)}. Run again.")
            return deals, errors, left
        try:
            rec, d = api.refresh_month(month, root, **({"fetch": fetch} if fetch else {}))
            deals[month] = d
            print(f"months: {month} ok n={rec['n']}")
        except SqlError as e:
            errors.append(f"{month}: {e}")
            print(f"months: {month} failed ({e}), keeping previous")
    return deals, errors, []


# ---------- בנייה מתוך קבצי החודש ----------

def pack(day_rows, anchor_month):
    """דחיסה: רשימת ימים, רשימת ערים, ושורות [יום, עיר, חדרים, n, סכום, מינ', מקס']."""
    out = []
    for r in day_rows:
        if not (isinstance(r, list) and len(r) == 7):
            continue
        d, city, rooms, n, s, mn, mx = r
        rooms = num(rooms) or 0
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(d)) or not city or not n or n <= 0:
            continue
        out.append([d, str(city)[:40], rooms if 0 <= rooms <= 20 else 0, int(n), round(s), round(mn), round(mx)])
    day_list = sorted({r[0] for r in out})
    city_list = sorted({r[1] for r in out})
    di = {d: i for i, d in enumerate(day_list)}
    ci = {c: i for i, c in enumerate(city_list)}
    packed = sorted([di[r[0]], ci[r[1]], r[2], r[3], r[4], r[5], r[6]] for r in out)
    anchor_days = [d for d in day_list if d.startswith(anchor_month)]
    return {"days": day_list, "cities": city_list, "rows": packed, "anchor": anchor_days[-1] if anchor_days else None}


def build_budget(anchor_month, recs):
    """12 חודשים עד חודש העוגן: לכל עיר, כמה דירות נמכרו עד התקציב, ובאיזה גודל ומחיר.
    עסקה עם שטח סביר שמחירה למ"ר נמוך מ-PPM_FLOOR מהממוצע של העיר באותה תקופה לא נספרת."""
    months = months_window(anchor_month, back=12)
    missing = [m for m in months if not isinstance((recs.get(m) or {}).get("budget"), dict)]
    if missing:
        raise SqlError(f"budget: missing months {', '.join(missing)}")
    lo, hi = api.AREA_OK
    cities = {}
    for m in months:
        for city, (n_all, s_area, area, deals) in recs[m]["budget"].items():
            c = cities.setdefault(city, [0, 0, 0, []])
            c[0] += n_all
            c[1] += s_area
            c[2] += area
            c[3].extend(deals)
    rows = []
    for city in sorted(cities):
        n_all, s_area, area, deals = cities[city]
        ref = s_area / area if area > 0 else None
        n_b = s_b = na_b = a_b = sa_b = 0
        for amt, ar in deals:
            ok_area = lo <= ar <= hi
            if ref and ok_area and amt < PPM_FLOOR * ref * ar:
                continue
            n_b += 1
            s_b += amt
            if ok_area:
                na_b += 1
                a_b += ar
                sa_b += amt
        if n_all > 0:
            rows.append([city, n_all, n_b, round(s_b), na_b, round(a_b), round(sa_b)])
    return {"from": months[0], "to": months[-1], "limit": BUDGET, "rows": rows,
            "ppm_filter": True, "ppm_floor": PPM_FLOOR}


def build_monthly(anchor_month, recs):
    """48 חודשים עד חודש העוגן: ספירה, סכום, מינ' ומקס' לפי חודש, עיר וקבוצת חדרים (0 = לא ידוע, 2 = עד 2.5, 3, 4, 5+)."""
    months = months_window(anchor_month, back=MONTHLY_BACK)
    cities, rows = {}, []
    for mi, m in enumerate(months):
        for city, rb, n, s, mn, mx in (recs.get(m) or {}).get("monthly") or []:
            if rb not in (0, 2, 3, 4, 5) or not n or n <= 0 or mx < mn:
                continue
            ci = cities.setdefault(city, len(cities))
            rows.append([mi, ci, int(rb), int(n), round(s), round(mn), round(mx)])
    names = [None] * len(cities)
    for k, v in cities.items():
        names[v] = k
    return {"months": months, "cities": names, "rows": rows}


WATCH_FILE = os.path.join(ROOT, "watch.json")
WATCH_MONTHS = 3
MIN_COMPS = 5                 # לפחות 5 עסקאות להשוואה (כלל "הזדמנות" של הפרויקט)
COMP_AREA = 0.20              # דומה = אותה קבוצת חדרים ושטח בטווח ±20%
SQM_PER_ROOM = (12, 45)       # מ"ר לחדר מחוץ לטווח: כנראה שגיאה בשטח או במספר החדרים
SAFE_NAME = re.compile(r"^[֐-׿0-9 \-'\"().]{2,40}$")


def load_watch():
    try:
        with open(WATCH_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return None
    cities = [str(c).strip() for c in cfg.get("cities", []) if SAFE_NAME.match(str(c).strip())][:12]
    pct = cfg.get("below_median_pct", 15)
    mx = cfg.get("max_flags_per_city", 3)
    pct = pct if isinstance(pct, (int, float)) and 5 <= pct <= 50 else 15
    mx = mx if isinstance(mx, int) and 0 <= mx <= 10 else 3
    cap = cfg.get("max_below_pct", 45)
    cap = cap if isinstance(cap, (int, float)) and pct < cap <= 90 else 45
    return {"cities": cities, "pct": pct, "cap": cap, "max_flags": mx} if cities else None


def _med(vals):
    vals = sorted(vals)
    if not vals:
        return None
    m = len(vals) // 2
    return vals[m] if len(vals) % 2 else (vals[m - 1] + vals[m]) / 2


def plausible(x):
    """עסקה שאפשר להשוות: מספר חדרים ידוע ושטח סביר למספר החדרים."""
    if not x["rooms"] or not x["rb"]:
        return False
    return SQM_PER_ROOM[0] <= x["area"] / x["rooms"] <= SQM_PER_ROOM[1]


def similar(x, pool):
    """עסקאות דומות: אותה עיר וקבוצת חדרים, שטח בטווח ±COMP_AREA, בלי העסקה עצמה."""
    lo, hi = x["area"] * (1 - COMP_AREA), x["area"] * (1 + COMP_AREA)
    return [c for c in pool if c is not x and c["city"] == x["city"] and c["rb"] == x["rb"] and lo <= c["area"] <= hi]


def fetch_watch(anchor_month, cfg, deals):
    """ערי מעקב: עסקאות של 3 החודשים המלאים האחרונים (deals: עסקאות נקיות מ-deals_api.clean_deal).
    חציון מחיר למ"ר לפי עיר וקבוצת חדרים, ועסקאות בחודש העוגן שנסגרו לפחות pct% מתחת לחציון של עסקאות דומות
    (אותה קבוצת חדרים, שטח ±20%, לפחות MIN_COMPS). עסקאות עם שטח לא סביר למספר החדרים לא נכנסות לחציונים ולא מסומנות."""
    months = months_window(anchor_month, back=WATCH_MONTHS)
    lo, hi = api.AREA_OK
    pool, seen = [], set()
    for d in deals:
        key = (d["date"], d["city"], d["gush"], d["helka"], d["sub"], d["amount"], d["area"], d["rooms"])
        if key in seen or d["date"][:7] not in months or d["city"] not in cfg["cities"] or not (lo <= d["area"] <= hi):
            continue
        seen.add(key)
        x = dict(d, ppm=d["amount"] / d["area"])
        x["plausible"] = plausible(x)
        pool.append(x)
    pool.sort(key=lambda x: (x["date"], x["city"], x["gush"], x["helka"], x["sub"], x["amount"], x["area"]))
    out = []
    for city in cfg["cities"]:
        cd = [x for x in pool if x["city"] == city]
        ok = [x for x in cd if x["plausible"]]
        med_all = _med([x["ppm"] for x in ok])
        by_rb = {}
        for rb in (2, 3, 4, 5):
            v = [x["ppm"] for x in ok if x["rb"] == rb]
            if len(v) >= MIN_COMPS:
                by_rb[str(rb)] = round(_med(v))
        flags, outliers, implausible = [], 0, 0
        for x in cd:
            if not x["date"].startswith(anchor_month):
                continue
            if not x["plausible"]:
                implausible += 1
                continue
            comps = similar(x, ok)
            if len(comps) < MIN_COMPS:
                continue
            ref = _med([c["ppm"] for c in comps])
            pct = x["ppm"] / ref - 1
            if pct < -cfg["cap"] / 100:
                outliers += 1  # זול מדי: כנראה לא עסקת שוק (משפחה, דייר מוגן, שגיאת שטח)
            elif pct <= -cfg["pct"] / 100:
                flags.append({"date": x["date"], "rooms": x["rooms"], "rb": x["rb"], "area": round(x["area"]),
                              "amount": x["amount"], "ppm": round(x["ppm"]), "gush": x["gush"], "chelka": x["helka"],
                              "sub": x["sub"], "year": x["year"], "ref": round(ref), "n_comps": len(comps),
                              "pct": round(pct * 100)})
        flags.sort(key=lambda f: (f["pct"], f["date"], f["gush"], f["chelka"], f["sub"]))
        out.append({"name": city, "n_ref": len(ok), "median_ppm": round(med_all) if med_all else None,
                    "median_by_rooms": by_rb, "flags": flags[:cfg["max_flags"]],
                    "flags_total": len(flags), "outliers": outliers, "implausible": implausible})
    return {"ref_months": months, "anchor_month": anchor_month, "below_pct": cfg["pct"], "cap_pct": cfg["cap"], "cities": out}


# ---------- נתונים קודמים ----------

def load_previous(path=OUT):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def keep_previous(name, prev):
    """כשאיסוף של חלק נכשל: מחזיר את הגרסה הקודמת שלו, מסומנת stale עם התאריך שבו נבנתה. אין גרסה קודמת: None."""
    old = prev.get(name)
    if not isinstance(old, dict):
        return None
    out = dict(old)
    out["stale"] = True
    out["stale_since"] = old.get("stale_since") or prev.get("generated_at")
    return out


def load_counts(root):
    recs = {m: api.load_month(m, root) for m in api.list_months(root)}
    recs = {m: r for m, r in recs.items() if r}
    return recs, {m: int(r.get("n") or 0) for m, r in recs.items()}


def build(today=None, prev=None, root=None, fetch=None):
    prev = prev if prev is not None else {}
    root = root or api.MONTHS_DIR
    st = api.stats()
    last = str(st.get("last_deal") or "")[:7]
    if not re.fullmatch(r"\d{4}-\d{2}", last):
        raise SqlError("stats: no last_deal")
    migrate_legacy(prev, root)
    # עוגן משוער לפני השליפה, כדי למשוך גם את חודשי המעקב: המעקב צריך עסקאות בודדות, והן לא נשמרות בקבצים
    early = pick_anchor_month(load_counts(root)[1], today)
    extra = months_window(early, back=WATCH_MONTHS) if early and load_watch() else ()
    fresh, errors, left = refresh(last, root, fetch, extra)

    recs, counts = load_counts(root)
    anchor_month = pick_anchor_month(counts, today)
    if not anchor_month:
        raise SqlError("no full month")
    later = [m for m in counts if m > anchor_month and counts[m] > 0]
    detail_months = months_window(anchor_month, extra_after=later)
    no_days = [m for m in detail_months if not (recs.get(m) or {}).get("days")]
    if no_days:
        raise SqlError(f"detail: missing months {', '.join(no_days)}")
    data = pack([r for m in detail_months for r in recs[m]["days"]], anchor_month)

    try:
        budget = build_budget(anchor_month, recs)
    except SqlError as e:
        print(f"budget: {e}, keeping previous")
        budget = keep_previous("budget", prev)
    monthly = build_monthly(anchor_month, recs)
    watch = None
    cfg = load_watch()
    if cfg:
        wm = months_window(anchor_month, back=WATCH_MONTHS)
        if all(m in fresh for m in wm):
            watch = fetch_watch(anchor_month, cfg, [d for m in wm for d in fresh[m]])
        else:
            print(f"watch: months {', '.join(m for m in wm if m not in fresh)} not fetched in this run, keeping previous")
            watch = keep_previous("watch", prev)
    return {
        "build_version": BUILD_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "רשות המסים, דרך גרסאות לעם (over.org.il)",
        "natures": list(APT_NATURES),
        "anchor_month": anchor_month,
        "anchor": data["anchor"],
        "months": [{"ym": m, "n": counts[m]} for m in sorted(counts)][-14:],
        "db_scraped_at": str(st.get("scraped_at") or "")[:19] or None,
        "db_last_deal": str(st.get("last_deal") or "")[:10] or None,
        "fetch_errors": errors,
        "months_missing": left,
        "days": data["days"],
        "cities": data["cities"],
        "rows": data["rows"],
        "budget": budget,
        "monthly": monthly,
        "watch": watch,
    }


def main():
    api.start_clock(BUDGET_SECONDS)
    report = build(prev=load_previous())
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, OUT)
    print(f"dashboard: anchor={report['anchor']} days={len(report['days'])} "
          f"cities={len(report['cities'])} rows={len(report['rows'])} errors={len(report['fetch_errors'])}")


if __name__ == "__main__":
    try:
        main()
    except SqlError as e:
        print(f"dashboard: failed ({e})")
        raise SystemExit(1)
