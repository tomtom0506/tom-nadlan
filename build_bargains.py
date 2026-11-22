#!/usr/bin/env python3
"""tom-nadlan / build_bargains.py
לשונית "מציאות": שני מקורות פתוחים של "גרסאות לעם" (over.org.il), בלי טוקן ובלי scraping.

1. עסקאות GovMap (/api/deals/govmap/search): אותן עסקאות שמציג אתר הנדל"ן הממשלתי, ומתעדכנות מהר
   יותר ממאגר רשות המסים. לכל עיר ב-bargains.json:
     - עסקאות מהימים האחרונים שנסגרו 15%–45% מתחת לחציון למ"ר של עסקאות דומות (אותה עיר, אותה קבוצת
       חדרים, שטח ±20%, 12 חודשים). אותם כללים כמו בלשונית "מעקב".
     - רחובות שבהם המחיר למ"ר נמוך מהחציון של העיר, ויש בהם עסקאות בתוך התקציב.
     - טבלת השוואה למחשבון "האם המחיר הזה מציאה": אחוזונים של מחיר למ"ר לפי קבוצת חדרים וטווח שטח.
2. מכרזי רמ"י (/api/michrazim/lots): מכרזים פתוחים (מועד ההגשה לא עבר) לבנייה למגורים, מקובצים
   לפי מכרז, עם מחיר ממוצע ליחידה ממכרזים קודמים באותו יישוב שבהם היה זוכה.

עסקאות GovMap נשמרות במטמון מקומי (.cache/govmap, לא ב-git). בריצה עם מטמון נשלפים רק 45 הימים
האחרונים לכל עיר. בלי מטמון נשלפים 12 חודשים מלאים.
בלוגים נרשם רק סוג השגיאה. אין סודות. מכרזים: לא נשמרים שמות זוכים או מספרי זהות של מציעים.
"""
import json
import os
import re
import statistics
import sys
from datetime import date, datetime, timedelta, timezone

import deals_api as api

BARGAINS_VERSION = "1.9.0"
ROOT = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(ROOT, "bargains.json")
OUT = os.path.join(ROOT, "data", "bargains.json")
CACHE_DIR = os.path.join(ROOT, ".cache", "govmap")
TIME_BUDGET = 16 * 60          # ה-job מוגבל ל-25 דקות
INCREMENTAL_DAYS = 45          # עסקאות מתעדכנות באיחור: שולפים מחדש את התקופה הזו בכל ריצה
APT_NATURES = ("דירה בבית קומות", "דירת גן", "דירת גג", "דירה", "דירת נופש", "דירה בבית קומות - דירה בקוטג'")
APT_TYPES = ("דירה", "דירת גן", "דירת גג", "דירה בבית קומות")
AREA_OK = (25, 300)
SQM_PER_ROOM = (12, 45)
PPM_FLOOR, PPM_CEIL = 0.40, 3.0     # מול החציון למ"ר של העיר
PROJECT_MIN = 8                     # עסקאות בחלקה אחת בלי רחוב = פרויקט חדש
PROJECT_DISCOUNT_PCT = 20           # פרויקט שזול ב-20%+ מהשוק = כנראה מחיר מפוקח
AREA_BANDS = (40, 55, 70, 85, 100, 120, 150)   # גבולות עליונים; מעל 150 = קבוצה אחרונה
RESIDENTIAL_PURPOSES = ("בנייה נמוכה/צמודת קרקע", "בנייה רוויה", "דיור להשכרה", "מגורים ו/או מסחר ו/או מלונאות ו/או נופש")
HIST_YEARS = 3


# ---------- עזר ----------

def norm(name):
    """שם יישוב לצורך השוואה: בלי גרשיים ומקפים, קריית=קרית, רווחים אחידים."""
    s = re.sub(r"[\"'׳״\-–]", " ", str(name or ""))
    s = s.replace("קריית", "קרית").replace("פתח תקוה", "פתח תקווה")
    return re.sub(r"\s+", " ", s).strip()


def num(v):
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def median(vals):
    vals = [v for v in vals if v is not None]
    return statistics.median(vals) if vals else None


def pctile(sorted_vals, p):
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def rooms_bucket(r):
    if not r or r <= 0:
        return 0
    return 2 if r < 3 else 3 if r < 4 else 4 if r < 5 else 5


def area_band(a):
    for i, top in enumerate(AREA_BANDS):
        if a <= top:
            return i
    return len(AREA_BANDS)


def load_cfg(path=CFG_PATH):
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    cities = [str(c).strip()[:40] for c in cfg.get("cities", []) if str(c).strip()]
    cfg["cities"] = list(dict.fromkeys(cities))[:40]
    for k, d in (("budget", 1_500_000), ("recent_days", 60), ("comps_months", 12), ("below_pct", 15),
                 ("max_below_pct", 45), ("min_comps", 5), ("max_flags_per_city", 8),
                 ("streets_per_city", 8), ("min_street_deals", 5)):
        v = num(cfg.get(k))
        cfg[k] = v if v is not None and v > 0 else d
    return cfg


# ---------- עסקאות GovMap ----------

def clean_deal(d, today):
    """עסקת דירה תקינה, או None. תאריכים עתידיים (יש כאלה במקור) נפסלים."""
    day = str(d.get("deal_date") or "")[:10]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) or day > today.isoformat():
        return None
    nature, ptype = d.get("deal_nature"), d.get("property_type")
    if nature is not None and nature not in APT_NATURES:
        return None
    if ptype is not None and ptype not in APT_TYPES:
        return None
    amt, area, rooms = num(d.get("deal_amount")), num(d.get("asset_area")), num(d.get("rooms"))
    if amt is None or not (150_000 <= amt <= 40_000_000):
        return None
    if area is None or not (AREA_OK[0] <= area <= AREA_OK[1]) or rooms is None or not (1 <= rooms <= 8):
        return None
    if not (SQM_PER_ROOM[0] <= area / rooms <= SQM_PER_ROOM[1]):
        return None
    did = d.get("deal_id") or d.get("objectid")
    if did is None:
        return None
    return {"id": str(did)[:24], "date": day, "amount": int(round(amt)), "area": round(area, 1), "rooms": rooms,
            "street": str(d.get("street") or "").strip()[:40], "house": str(d.get("house_num") or "").strip()[:8],
            "floor": str(d.get("floor") or "").strip()[:16], "gush": str(d.get("gush") or "")[:10],
            "helka": str(d.get("parcel") or "")[:10]}


def govmap_names(all_names, city):
    """כל הכתיבים של עיר במקור (למשל "תל אביב-יפו" ו-"תל אביב -יפו")."""
    want = norm(city)
    return sorted({n for n in all_names if norm(n) == want})


def fetch_settlement_names():
    data = api.get_json("/api/deals/govmap/settlements", {"limit": 2000})
    rows = data.get("data")
    if not isinstance(rows, list):
        raise api.ApiError("NoData")
    return [str(r.get("settlement")) for r in rows if isinstance(r, dict) and r.get("settlement")]


def fetch_deals(settlement, date_from, date_to):
    """כל העסקאות של כתיב אחד בטווח תאריכים, בדפים של 200, בלי כפילויות לפי deal_id."""
    out, seen, offset = [], set(), 0
    while True:
        if offset > api.MAX_OFFSET:
            raise api.ApiError("offset limit")
        data = api.get_json("/api/deals/govmap/search", {
            "settlement": settlement, "date_from": date_from, "date_to": date_to,
            "sort": "date_desc", "limit": api.PAGE, "offset": offset})
        page = data.get("data")
        if not isinstance(page, list):
            raise api.ApiError("NoData")
        if data.get("total_capped"):
            raise api.ApiError("total capped")
        for d in page:
            if isinstance(d, dict):
                k = d.get("deal_id") or d.get("objectid")
                if k not in seen:
                    seen.add(k)
                    out.append(d)
        total = data.get("total") if isinstance(data.get("total"), int) else None
        offset += api.PAGE
        if len(page) < api.PAGE or (total is not None and offset >= total):
            return out


def cache_path(city, root=CACHE_DIR):
    return os.path.join(root, re.sub(r"[^\w]+", "_", norm(city)) + ".json")


def load_cache(city, root=CACHE_DIR):
    try:
        with open(cache_path(city, root), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) and isinstance(d.get("deals"), list) else None
    except (OSError, ValueError):
        return None


def save_cache(city, rec, root=CACHE_DIR):
    os.makedirs(root, exist_ok=True)
    p = cache_path(city, root)
    with open(p + ".tmp", "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(p + ".tmp", p)


def city_deals(city, names, today, months, root=CACHE_DIR, fetch=fetch_deals):
    """עסקאות העיר ל-months חודשים. עם מטמון טרי (עד 10 ימים) נשלפים רק INCREMENTAL_DAYS הימים האחרונים."""
    start = (today - timedelta(days=int(months * 30.5))).isoformat()
    cache = load_cache(city, root)
    fresh = cache and cache.get("names") == names and cache.get("start", "9") <= start and \
        cache.get("fetched", "") >= (today - timedelta(days=10)).isoformat()
    date_from = (today - timedelta(days=INCREMENTAL_DAYS)).isoformat() if fresh else start
    got = {}
    for n in names:
        for d in fetch(n, date_from, today.isoformat()):
            x = clean_deal(d, today)
            if x:
                got[x["id"]] = x
    keep = {x["id"]: x for x in (cache["deals"] if fresh else []) if x.get("date", "") >= start and x["date"] < date_from}
    keep.update(got)
    deals = sorted(keep.values(), key=lambda x: (x["date"], x["id"]))
    save_cache(city, {"city": city, "names": names, "start": start, "fetched": today.isoformat(), "deals": deals}, root)
    return deals, ("incremental" if fresh else "full")


# ---------- ניתוח ----------

def ppm(x):
    return x["amount"] / x["area"]


def comps_for(x, pool):
    rb, a = rooms_bucket(x["rooms"]), x["area"]
    return [ppm(y) for y in pool if y["id"] != x["id"] and rooms_bucket(y["rooms"]) == rb and abs(y["area"] - a) <= a * 0.2]


def find_flags(deals, cfg, today):
    """עסקאות מהימים האחרונים שנסגרו below_pct%–max_below_pct% מתחת לחציון של עסקאות דומות, בתוך התקציב."""
    since = (today - timedelta(days=int(cfg["recent_days"]))).isoformat()
    lo, hi = 1 - cfg["max_below_pct"] / 100, 1 - cfg["below_pct"] / 100
    flags, outliers = [], 0
    for x in deals:
        if x["date"] < since or x["amount"] > cfg["budget"]:
            continue
        comps = comps_for(x, deals)
        if len(comps) < cfg["min_comps"]:
            continue
        ref = statistics.median(comps)
        r = ppm(x) / ref
        if r < lo:
            outliers += 1
            continue
        if r <= hi:
            flags.append(dict(x, ppm=round(ppm(x)), ref=round(ref), pct=round((r - 1) * 100), n_comps=len(comps)))
    flags.sort(key=lambda f: (f["pct"], f["date"]))
    return flags, outliers


def street_stats(deals, cfg, city_med):
    by = {}
    for x in deals:
        if x["street"]:
            by.setdefault(x["street"], []).append(x)
    rows = []
    for st, xs in by.items():
        if len(xs) < cfg["min_street_deals"]:
            continue
        in_budget = [x for x in xs if x["amount"] <= cfg["budget"]]
        if not in_budget:
            continue
        m = statistics.median(ppm(x) for x in xs)
        rows.append({"street": st, "n": len(xs), "ppm": round(m), "vs_city": round((m / city_med - 1) * 100) if city_med else 0,
                     "n_budget": len(in_budget), "budget_area": round(statistics.median(x["area"] for x in in_budget)),
                     "budget_price": round(statistics.median(x["amount"] for x in in_budget)), "last": max(x["date"] for x in xs)})
    rows.sort(key=lambda r: (r["vs_city"], -r["n_budget"]))
    return [r for r in rows if r["vs_city"] < 0][:int(cfg["streets_per_city"])]


def price_table(deals):
    """אחוזונים 25/50/75 של מחיר למ"ר לפי קבוצת חדרים וטווח שטח: {rb: {band: [n, p25, p50, p75]}}."""
    t = {}
    for x in deals:
        t.setdefault(str(rooms_bucket(x["rooms"])), {}).setdefault(str(area_band(x["area"])), []).append(ppm(x))
    out = {}
    for rb, bands in t.items():
        for b, vals in bands.items():
            if len(vals) >= 5:
                vals.sort()
                out.setdefault(rb, {})[b] = [len(vals), round(pctile(vals, .25)), round(pctile(vals, .5)), round(pctile(vals, .75))]
    return out


def drop_implausible(deals):
    """ב-GovMap אין שדה "חלק בנכס", ולכן מכירה של חלק מדירה נראית כמו דירה זולה מאוד.
    עסקה שמחירה למ"ר נמוך מ-PPM_FLOOR מהחציון של העיר (או גבוה מ-PPM_CEIL ממנו) לא נספרת בשום חישוב."""
    if not deals:
        return deals, 0
    med = statistics.median(ppm(x) for x in deals)
    ok = [x for x in deals if PPM_FLOOR * med <= ppm(x) <= PPM_CEIL * med]
    return ok, len(deals) - len(ok)


def split_projects(deals):
    """פרויקט חדש: חלקה עם לפחות PROJECT_MIN עסקאות בחלון, בלי כתובת רחוב (הבניין עוד לא רשום).
    אלה מכירות של יזם, לרוב בבת אחת, ולעתים במחיר מפוקח (דירה בהנחה, מחיר מטרה). הן לא עסקאות שוק
    רגילות, ולכן לא נכנסות לחציונים, למגמה ולמציאות, ומוצגות בנפרד."""
    by = {}
    for x in deals:
        if x["gush"] and x["helka"]:
            by.setdefault((x["gush"], x["helka"]), []).append(x)
    proj_keys = {k for k, xs in by.items() if len(xs) >= PROJECT_MIN and sum(1 for x in xs if not x["street"]) >= len(xs) * 0.8}
    market = [x for x in deals if (x["gush"], x["helka"]) not in proj_keys]
    return market, {k: by[k] for k in proj_keys}


def project_rows(projects, city_med, cfg):
    rows = []
    for (gush, helka), xs in projects.items():
        m = statistics.median(ppm(x) for x in xs)
        vs = round((m / city_med - 1) * 100) if city_med else 0
        rows.append({"gush": gush, "helka": helka, "n": len(xs), "ppm": round(m), "vs_city": vs,
                     "price": round(statistics.median(x["amount"] for x in xs)), "area": round(statistics.median(x["area"] for x in xs)),
                     "rooms": statistics.median(x["rooms"] for x in xs), "first": min(x["date"] for x in xs), "last": max(x["date"] for x in xs),
                     "discount": vs <= -PROJECT_DISCOUNT_PCT})
    rows.sort(key=lambda r: (not r["discount"], r["vs_city"]))
    return rows[:int(cfg.get("projects_per_city", 6))]


def price_trend(deals, today):
    """שינוי במחיר למ"ר: 3 החודשים האחרונים מול 9 החודשים שלפניהם, בכל קבוצת חדרים בנפרד, ממוצע משוקלל.
    כך שינוי בתמהיל (למשל יותר דירות קטנות) לא נראה כמו שינוי מחיר."""
    q3 = (today - timedelta(days=91)).isoformat()
    num_, den = 0.0, 0
    for rb in (2, 3, 4, 5):
        rec = [ppm(x) for x in deals if rooms_bucket(x["rooms"]) == rb and x["date"] >= q3]
        old = [ppm(x) for x in deals if rooms_bucket(x["rooms"]) == rb and x["date"] < q3]
        if len(rec) >= 10 and len(old) >= 20:
            num_ += (statistics.median(rec) / statistics.median(old) - 1) * len(rec)
            den += len(rec)
    return round(num_ / den * 100, 1) if den >= 20 else None


def city_summary(city, deals, cfg, today):
    deals, projects = split_projects(deals)
    deals, implausible = drop_implausible(deals)
    if not deals:
        return {"name": city, "n": 0, "implausible": implausible}
    city_med = statistics.median(ppm(x) for x in deals)
    trend = price_trend(deals, today)
    in_budget = [x for x in deals if x["amount"] <= cfg["budget"]]
    flags, outliers = find_flags(deals, cfg, today)
    keep = ("id", "date", "amount", "area", "rooms", "street", "house", "floor", "gush", "helka", "ppm", "ref", "pct", "n_comps")
    return {
        "name": city, "n": len(deals), "last_deal": deals[-1]["date"], "median_ppm": round(city_med), "trend_pct": trend,
        "n_budget": len(in_budget), "budget_share": round(len(in_budget) / len(deals), 3),
        "budget_area": round(statistics.median(x["area"] for x in in_budget)) if in_budget else None,
        "flags": [{k: f[k] for k in keep} for f in flags[:int(cfg["max_flags_per_city"])]], "flags_total": len(flags),
        "outliers": outliers, "implausible": implausible,
        "project_deals": sum(len(v) for v in projects.values()), "projects": project_rows(projects, city_med, cfg), "streets": street_stats(deals, cfg, city_med), "table": price_table(deals),
    }


# ---------- מכרזי רמ"י ----------

def tender_kind(lot):
    t, p = lot.get("tender_type") or "", lot.get("purpose") or ""
    units = num(lot.get("units")) or 0
    if "הרשמה והגרלה" in t:
        return "lottery"
    if "מחיר מטרה" in t or "מחיר למשתכן" in t or "דירה בהנחה" in t:
        return "target"
    if p == "בנייה נמוכה/צמודת קרקע" and 0 < units <= 2:
        return "self"
    return "dev"


def fetch_lots(params):
    out, offset = [], 0
    while True:
        data = api.get_json("/api/michrazim/lots", dict(params, limit=api.PAGE, offset=offset))
        page = data.get("data")
        if not isinstance(page, list):
            raise api.ApiError("NoData")
        out += [d for d in page if isinstance(d, dict)]
        total = data.get("total") if isinstance(data.get("total"), int) else None
        offset += api.PAGE
        if len(page) < api.PAGE or (total is not None and offset >= total) or offset > 5000:
            return out


PUBLIC = "כלל הציבור"
NO_LIMIT = "ללא הגבלה"   # מגרש בלי אוכלוסייה מוגדרת (למשל מכרז פומבי רגיל): פתוח לכולם


def lot_pops(l):
    """האוכלוסיות שהמגרש מיועד להן, ממוינות. בלי הגדרה: NO_LIMIT."""
    pops = sorted({p.strip()[:40] for p in str(l.get("eligible_populations") or "").split(";") if p.strip()})
    return pops or [NO_LIMIT]


def open_to(groups, mine=()):
    """כמה מגרשים ויחידות במכרז פתוחים למי שמשתייך לאוכלוסיות mine (כלל הציבור ומגרשים בלי הגבלה תמיד כלולים)."""
    allowed = {PUBLIC, NO_LIMIT} | set(mine)
    lots = units = 0
    for g in groups or []:
        if allowed & set(g.get("pops") or []):
            lots += g.get("lots") or 0
            units += g.get("units") or 0
    return lots, units


def group_tenders(lots, today):
    """מכרזים פתוחים למגורים, אחד לכל מכרז. בלי שמות זוכים ומציעים."""
    now = today.isoformat()
    by = {}
    for l in lots:
        dl = str(l.get("deadline") or "")[:10]
        if l.get("purpose") not in RESIDENTIAL_PURPOSES or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", dl) or dl < now:
            continue
        tid = str(l.get("tender_id") or "")[:12]
        if not tid:
            continue
        t = by.setdefault(tid, {"id": tid, "number": str(l.get("tender_number") or "")[:16],
                                "city": str(l.get("settlement") or "").strip()[:40], "kind": tender_kind(l),
                                "type": str(l.get("tender_type") or "")[:60], "purpose": str(l.get("purpose") or "")[:60],
                                "region": str(l.get("region") or "")[:30], "neighborhood": str(l.get("neighborhood") or "")[:60],
                                "published": str(l.get("published") or "")[:10], "deadline": dl,
                                "lottery_date": str(l.get("lottery_date") or "")[:10] or None,
                                "url": l.get("tender_url") if str(l.get("tender_url") or "").startswith("https://apps.land.gov.il/") else None,
                                "lots": 0, "units": 0, "_appr": [], "_dev": [], "_thr": [], "_groups": {}})
        t["lots"] += 1
        units = num(l.get("units")) or 0
        t["units"] += int(units)
        g = t["_groups"].setdefault(tuple(lot_pops(l)), [0, 0])
        g[0] += 1
        g[1] += int(units)
        if units > 0:
            for key, field in (("_appr", "appraisal_price"), ("_dev", "dev_costs"), ("_thr", "threshold_price")):
                v = num(l.get(field))
                if v and v > 1:
                    t[key].append(v / units)
        if tender_kind(l) in ("self", "lottery") and t["kind"] == "dev":
            t["kind"] = tender_kind(l)
    out = []
    for t in by.values():
        for key, name in (("_appr", "appraisal_per_unit"), ("_dev", "dev_per_unit"), ("_thr", "threshold_per_unit")):
            vals = t.pop(key)
            t[name] = round(min(vals)) if vals else None
        # אוכלוסיות לפי מגרש: מכרז אחד יכול לכלול מגרשים לכלל הציבור ומגרשים לחיילי מילואים או לאנשים עם מוגבלות
        t["groups"] = [{"pops": list(p), "lots": v[0], "units": v[1]} for p, v in sorted(t.pop("_groups").items())]
        pops = sorted({p for g in t["groups"] for p in g["pops"]} - {NO_LIMIT})
        t["eligible"] = "; ".join(pops)[:120] or None
        t["open_lots"], t["open_units"] = open_to(t["groups"])
        out.append(t)
    out.sort(key=lambda t: (t["deadline"], t["city"]))
    return out


def tender_history(city, fetch=fetch_lots, today=None):
    """מכרזים קודמים ביישוב (3 שנים) עם זוכה: מחיר קרקע + פיתוח ליחידה, ומספר הצעות. רק בנייה נמוכה."""
    today = today or date.today()
    lots = fetch({"settlement": city, "won_only": "true", "year_from": today.year - HIST_YEARS, "amount_basis": "lot_total"})
    per_unit, bids = [], []
    for l in lots:
        if l.get("purpose") != "בנייה נמוכה/צמודת קרקע":
            continue
        a, d = num(l.get("amount_per_unit")), num(l.get("dev_costs_per_unit")) or 0
        if a and a > 1000:
            per_unit.append(a + d)
            b = num(l.get("bids"))
            if b is not None:
                bids.append(b)
    if len(per_unit) < 3:
        return None
    return {"n": len(per_unit), "per_unit": round(statistics.median(per_unit)), "bids": round(statistics.median(bids), 1) if bids else None}


# ---------- בנייה ----------

def build(today=None, cfg=None, cache_root=CACHE_DIR, fetch=fetch_deals, lots_fetch=fetch_lots, names=None):
    today = today or datetime.now(timezone.utc).date()
    cfg = cfg or load_cfg()
    errors, cities, modes = [], [], {}
    try:
        all_names = names if names is not None else fetch_settlement_names()
    except api.ApiError as e:
        all_names, errors = [], [f"settlements: {e}"]
    for city in cfg["cities"]:
        ns = govmap_names(all_names, city) if all_names else [city]
        if not ns:
            errors.append(f"{city}: not in GovMap")
            continue
        try:
            deals, modes[city] = city_deals(city, ns, today, cfg["comps_months"], cache_root, fetch)
            cities.append(city_summary(city, deals, cfg, today))
        except api.ApiError as e:
            errors.append(f"{city}: {e}")
            print(f"bargains: {city}: {e}")
    tenders, hist = [], {}
    try:
        lots = lots_fetch({"status": "מפורסם", "year_from": today.year - 1})
        tenders = group_tenders(lots, today)
        for c in sorted({t["city"] for t in tenders if t["kind"] in ("self", "lottery")}):
            try:
                h = tender_history(c, lots_fetch, today)
                if h:
                    hist[c] = h
            except api.ApiError as e:
                errors.append(f"tender history {c}: {e}")
    except api.ApiError as e:
        errors.append(f"tenders: {e}")
    for t in tenders:
        t["history"] = hist.get(t["city"])
    last = max((c.get("last_deal") or "" for c in cities), default="") or None
    return {
        "version": BARGAINS_VERSION, "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "GovMap ומכרזי רמ\"י, דרך גרסאות לעם (over.org.il)", "today": today.isoformat(), "last_deal": last,
        "budget": cfg["budget"], "recent_days": cfg["recent_days"], "comps_months": cfg["comps_months"],
        "below_pct": cfg["below_pct"], "max_below_pct": cfg["max_below_pct"], "min_comps": cfg["min_comps"],
        "area_bands": list(AREA_BANDS), "cities": cities, "tenders": tenders, "fetch_modes": modes, "errors": errors[:40],
    }


def keep_previous(new, prev):
    """עיר או מכרזים שלא נאספו הפעם נשמרים מהקובץ הקודם, עם stale."""
    if not prev:
        return new
    have = {c["name"] for c in new["cities"]}
    for c in prev.get("cities") or []:
        if c.get("name") not in have and c.get("name") in set(load_cfg()["cities"]):
            new["cities"].append(dict(c, stale=True))
    if not new["tenders"] and prev.get("tenders") and any(e.startswith("tenders:") for e in new["errors"]):
        today = new["today"]
        new["tenders"] = [dict(t, stale=True) for t in prev["tenders"] if t.get("deadline", "") >= today]
    return new


def main():
    api.start_clock(TIME_BUDGET)
    try:
        with open(OUT, encoding="utf-8") as f:
            prev = json.load(f)
    except (OSError, ValueError):
        prev = None
    out = keep_previous(build(), prev)
    if not out["cities"] and not out["tenders"]:
        print("bargains: nothing collected; keeping previous file")
        return 1
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT + ".tmp", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(OUT + ".tmp", OUT)
    print(f"bargains: {len(out['cities'])} cities, {sum(c.get('flags_total', 0) for c in out['cities'])} flags, "
          f"{len(out['tenders'])} open tenders, errors={len(out['errors'])}, modes={out['fetch_modes']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
