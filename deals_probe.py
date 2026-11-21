#!/usr/bin/env python3
"""tom-nadlan / deals_probe.py
בדיקת ה-API החדש של "גרסאות לעם" (/api/deals/...), אחרי שנקודת ה-SQL התחילה להחזיר 401 ב-08.11.
רץ ב-GitHub Actions בהרצה ידנית. קריאה בלבד, כ-14 בקשות עם השהיה ביניהן.

שומר ב-repo:
- data/deals_probe.json   לכל בקשה: קוד תשובה, זמן, שמות שדות, דוגמה מקוצרת, כותרות מגבלת קצב, CORS.
- data/over_api_doc.txt   הטקסט של דף התיעוד https://www.over.org.il/api ורשימת כל הנתיבים שמופיעים בו.
- data/over_openapi.json  מפרט OpenAPI, אם השרת חושף אחד.
אבטחה: אין סודות ואין קלט מהמשתמש. כל התוכן ציבורי. גוף שגיאה נשמר מקוצר ומנוקה.
"""
import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

PROBE_VERSION = "1.1.0"
BASE = "https://www.over.org.il"
ORIGIN = "https://tomtom0506.github.io"
ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "data", "deals_probe.json")
DOC_OUT = os.path.join(ROOT, "data", "over_api_doc.txt")
OPENAPI_OUT = os.path.join(ROOT, "data", "over_openapi.json")
DAY_OUT = os.path.join(ROOT, "data", "deals_probe_day.json")
DATASET_ID = "fd06f5ae-8a4f-4120-b275-8a514ad23499"
DUP_DAYS = ("2026-05-10", "2026-07-12", "2026-02-15")
DAY_FIELDS = ("deal_date", "settlement", "gush", "chelka", "sub_chelka", "deal_amount", "deal_nature", "portion",
              "asset_area", "room_num", "row_hash", "scraped_at", "first_seen", "last_seen")
UA = "tom-nadlan-probe/1.0 (personal, read-only)"
TIMEOUT = 30
PAUSE = 1.5
MAX_BODY = 4_000_000
DOC_MAX = 400_000
SAMPLE_LIST_MAX = 3
SAMPLE_STR_MAX = 300
LIMIT_HINTS = ("ratelimit", "rate-limit", "retry-after", "quota", "budget")
APT = "דירה בבית קומות"


def fetch(path, params=None, accept="application/json"):
    query = ("?" + urllib.parse.urlencode(params)) if params else ""
    req = urllib.request.Request(BASE + path + query, headers={"User-Agent": UA, "Accept": accept, "Origin": ORIGIN})
    t0 = time.monotonic()
    status, headers, body, error = None, {}, b"", None
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            status, headers, body = r.status, dict(r.headers.items()), r.read(MAX_BODY)
    except urllib.error.HTTPError as e:
        status, headers, error = e.code, dict((e.headers or {}).items()), "HTTPError"
        try:
            body = e.read(2000)
        except Exception:  # noqa: BLE001
            body = b""
    except Exception as e:  # noqa: BLE001 - רק סוג השגיאה
        error = type(e).__name__
    return {"status": status, "headers": {k.lower(): v for k, v in headers.items()}, "body": body,
            "ms": round((time.monotonic() - t0) * 1000), "error": error}


def clean(text, limit=SAMPLE_STR_MAX):
    text = re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]", " ", text)).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def parse_json(body):
    try:
        return json.loads(body.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None


def keys_of(obj):
    """שמות השדות ברמה העליונה, ובפריט הראשון של כל רשימה שבה."""
    if isinstance(obj, list):
        return {"type": "list", "count": len(obj), "item_keys": sorted(obj[0])[:60] if obj and isinstance(obj[0], dict) else []}
    if isinstance(obj, dict):
        out = {"type": "object", "keys": sorted(obj)[:60]}
        for k, v in obj.items():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                out.setdefault("lists", {})[k] = {"count": len(v), "item_keys": sorted(v[0])[:60]}
        return out
    return {"type": type(obj).__name__}


def trim(obj, depth=0):
    if depth > 10:
        return "…"
    if isinstance(obj, dict):
        return {k: trim(v, depth + 1) for k, v in list(obj.items())[:60]}
    if isinstance(obj, list):
        out = [trim(v, depth + 1) for v in obj[:SAMPLE_LIST_MAX]]
        if len(obj) > SAMPLE_LIST_MAX:
            out.append(f"… ועוד {len(obj) - SAMPLE_LIST_MAX}")
        return out
    if isinstance(obj, str) and len(obj) > SAMPLE_STR_MAX:
        return obj[:SAMPLE_STR_MAX] + "…"
    return obj


def html_to_text(raw):
    """טקסט קריא מדף HTML: בלי script/style, תגיות שורה הופכות לשורה חדשה."""
    s = re.sub(r"(?is)<(script|style|noscript|svg)\b.*?</\1>", " ", raw)
    s = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h[1-6]|tr|section|article|pre|code|dt|dd)>", "\n", s)
    s = html.unescape(re.sub(r"<[^>]+>", " ", s))
    lines = [re.sub(r"[ \t ]+", " ", ln).strip() for ln in s.splitlines()]
    out, blank = [], False
    for ln in lines:
        if ln:
            out.append(ln)
            blank = False
        elif not blank:
            out.append("")
            blank = True
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", "\n".join(out)).strip()


def api_paths(text):
    """כל הנתיבים מהצורה /api/... שמופיעים בטקסט, בלי כפילויות ובלי פרמטרים."""
    found = re.findall(r"/api/[A-Za-z0-9_\-/{}.]+", text)
    return sorted({p.rstrip("/.") for p in found})


def check(cid, label, path, params=None):
    res = fetch(path, params)
    data = parse_json(res["body"]) if res["body"] else None
    ok = res["status"] == 200 and data is not None
    c = {"id": cid, "label": label, "path": path, "params": params or {}, "ok": ok, "status": res["status"],
         "ms": res["ms"], "kb": round(len(res["body"]) / 1024, 1),
         "limits": {k: v for k, v in res["headers"].items() if any(h in k for h in LIMIT_HINTS)},
         "cors": res["headers"].get("access-control-allow-origin"),
         "content_type": res["headers"].get("content-type")}
    if ok:
        c["shape"] = keys_of(data)
        c["sample"] = trim(data)
    else:
        c["error"] = res["error"] or "BadJSON"
        c["error_body"] = clean(res["body"].decode("utf-8", "replace")) if res["body"] else None
        auth = res["headers"].get("www-authenticate")
        if auth:
            c["www_authenticate"] = clean(auth)
    print(f"[{cid}] {res['status']} {res['ms']}ms" + ("" if ok else f" {c['error']}"))
    time.sleep(PAUSE)
    return c, data


def total_of(data):
    """total מתשובת datastore_search (מעטפת CKAN) או מ-/api/deals/search."""
    if not isinstance(data, dict):
        return None
    if isinstance(data.get("result"), dict):
        return data["result"].get("total")
    return data.get("total")


def duplicates():
    """v1.6.0: ה-API מחזיר 10%-20% פחות עסקאות מה-SQL הישן. השערה: הטבלה היא ארכיון שמצטבר, וה-SQL ספר כמה
    דגימות של אותה עסקה. לכל יום: ספירה בארכיון (כל הדגימות, latest, current) מול total של /api/deals/search."""
    out, day_rows = [], None
    ds = f"/api/append/{DATASET_ID}/datastore_search"
    for day in DUP_DAYS:
        y, m, d = day.split("-")
        filt = json.dumps({"deal_date": f"{d}/{m}/{y}", "deal_nature": APT}, ensure_ascii=False)
        row = {"day": day}
        for name, extra in (("archive_all", {"limit": "1000"}), ("archive_latest", {"latest": "true", "limit": "1"}),
                            ("archive_current", {"current": "true", "limit": "1"})):
            c, data = check(f"dup_{day}_{name}", f"{day}: {name}", ds, dict(extra, filters=filt, include_total="true"))
            row[name] = total_of(data) if c["ok"] else f"error {c['status']}: {c.get('error_body')}"
            if name == "archive_all" and c["ok"] and day_rows is None:
                recs = (data.get("result") or {}).get("records") or []
                day_rows = {"day": day, "count": len(recs),
                            "records": [{k: r.get(k) for k in DAY_FIELDS} for r in recs if isinstance(r, dict)]}
        c, data = check(f"dup_{day}_api", f"{day}: /api/deals/search", "/api/deals/search",
                        {"nature": APT, "date_from": day, "date_to": day, "limit": "1"})
        row["api_total"] = total_of(data) if c["ok"] else f"error {c['status']}"
        out.append(row)
        print(f"[dup] {row}")
    c, log = check("deals_log", "יומן תיקונים /api/deals/log", "/api/deals/log")
    return out, day_rows, (log if c["ok"] else None)


def probe():
    checks = []
    doc = fetch("/api", accept="text/html,application/json")
    doc_text = ""
    if doc["status"] == 200 and doc["body"]:
        raw = doc["body"].decode("utf-8", "replace")
        maybe = parse_json(doc["body"])
        doc_text = json.dumps(maybe, ensure_ascii=False, indent=1) if maybe is not None else html_to_text(raw)
    checks.append({"id": "doc", "label": "דף התיעוד /api", "path": "/api", "ok": bool(doc_text), "status": doc["status"],
                   "ms": doc["ms"], "kb": round(len(doc["body"]) / 1024, 1), "error": doc["error"]})
    time.sleep(PAUSE)

    openapi = None
    for p in ("/openapi.json", "/api/openapi.json"):
        c, data = check("openapi" + p.replace("/", "_").replace(".json", ""), "מפרט OpenAPI " + p, p)
        c.pop("sample", None)
        checks.append(c)
        if c["ok"] and isinstance(data, dict) and "paths" in data:
            openapi = data
            break

    month = {"date_from": "2026-05-01", "date_to": "2026-05-31"}
    for cid, label, path, params in [
        ("stats", "מוני על וטריות", "/api/deals/stats", None),
        ("natures", "סוגי עסקאות", "/api/deals/natures", None),
        ("settlements", "יישובים", "/api/deals/settlements", None),
        ("search_small", "חיפוש: חיפה, דירה, 5 תוצאות", "/api/deals/search",
         {"settlement": "חיפה", "nature": APT, "date_from": "2026-05-01", "limit": "5"}),
        ("search_page", "חיפוש: כל הארץ, דירה, חודש, 200 תוצאות", "/api/deals/search",
         dict(month, nature=APT, limit="200", sort="date_asc")),
        ("search_offset", "חיפוש: אותו דבר, offset=200", "/api/deals/search",
         dict(month, nature=APT, limit="200", offset="200", sort="date_asc")),
        ("search_budget", "חיפוש: עד ₪1.5M, 3–4 חדרים", "/api/deals/search",
         dict(month, nature=APT, max_amount="1500000", min_rooms="3", max_rooms="4", limit="5")),
        ("series", "סדרה שנתית: חיפה, דירה", "/api/deals/series", {"settlement": "חיפה", "nature": APT}),
        ("breakdown", "פילוח לפי מהות: חיפה", "/api/deals/breakdown", {"settlement": "חיפה"}),
        ("parcel", "עסקאות בחלקה 7104/289", "/api/deals/parcel/7104/289", None),
        ("nadlan_stats", "API הישן של 'בדוק נכס'", "/api/nadlan/stats", None),
    ]:
        checks.append(check(cid, label, path, params)[0])

    dup, day_rows, log = duplicates()
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    if day_rows is not None or log is not None:
        with open(DAY_OUT, "w", encoding="utf-8") as f:
            json.dump({"day": day_rows, "log": log}, f, ensure_ascii=False, indent=1)
    if doc_text:
        paths = api_paths(doc_text)
        with open(DOC_OUT, "w", encoding="utf-8") as f:
            f.write(f"# {BASE}/api, נשמר {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n")
            f.write("# נתיבים שמופיעים בדף:\n" + "\n".join(paths) + "\n\n# טקסט הדף:\n")
            f.write(doc_text[:DOC_MAX])
    if openapi is not None:
        with open(OPENAPI_OUT, "w", encoding="utf-8") as f:
            json.dump(openapi, f, ensure_ascii=False, indent=1)
    return {"probe_version": PROBE_VERSION, "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "base": BASE, "doc_saved": bool(doc_text), "openapi_saved": openapi is not None, "duplicates": dup,
            "checks": checks}


def main():
    report = probe()
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    os.replace(tmp, OUT)
    ok = sum(1 for c in report["checks"] if c["ok"])
    print(f"deals_probe: {ok}/{len(report['checks'])} ok, doc={report['doc_saved']}, openapi={report['openapi_saved']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
