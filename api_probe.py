#!/usr/bin/env python3
"""tom-nadlan / api_probe.py
בדיקת היתכנות לממשק הציבורי של "גרסאות לעם" (over.org.il). קריאה בלבד.

- שולח 7 בקשות בלבד, עם השהיה ביניהן, כדי לא לשרוף את תקציב הנפח היומי.
- שומר את מבנה התשובות (שמות שדות, כמויות, זמני תגובה) ב-data/api_probe.json.
- שולח סיכום קצר לבוט הטלגרם, אם הסודות מוגדרים.
- אבטחה: בלוגים נרשם רק סוג השגיאה. טוקנים לא מודפסים לעולם.
"""
import json
import os
import re
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

PROBE_VERSION = "0.1.1"
BASE = "https://www.over.org.il"
ORIGIN = "https://tomtom0506.github.io"
APP_URL = "https://tomtom0506.github.io/tom-nadlan/"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "api_probe.json")
SAMPLES_OUT = os.path.join(os.path.dirname(OUT), "api_samples.json")
SAMPLE_LIST_MAX = 3
SAMPLE_STR_MAX = 300
TIMEOUT = 25
PAUSE = 1.5
MAX_BODY = 2_000_000
UA = "tom-nadlan-probe/0.1 (personal, read-only)"
LIMIT_HINTS = ("ratelimit", "rate-limit", "retry-after", "budget", "quota")
LIST_KEYS = ("deals", "results", "items", "data", "features", "streets", "rows")
FALLBACK_PARCEL = (6319, 225)  # החלקה שמופיעה כדוגמה בתיעוד הרשמי


def fetch(path, params=None):
    """בקשת GET אחת. מחזיר מילון אחיד גם בכישלון."""
    query = ("?" + urllib.parse.urlencode(params)) if params else ""
    req = urllib.request.Request(
        BASE + path + query,
        headers={"User-Agent": UA, "Accept": "application/json", "Origin": ORIGIN},
    )
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            body = r.read(MAX_BODY + 1)
            return {
                "status": r.status,
                "headers": {k.lower(): v for k, v in r.headers.items()},
                "body": body[:MAX_BODY],
                "truncated": len(body) > MAX_BODY,
                "ms": round((time.monotonic() - t0) * 1000),
                "error": None,
            }
    except urllib.error.HTTPError as e:
        return {
            "status": e.code,
            "headers": {k.lower(): v for k, v in (e.headers or {}).items()},
            "body": b"",
            "truncated": False,
            "ms": round((time.monotonic() - t0) * 1000),
            "error": "HTTPError",
        }
    except Exception as e:  # noqa: BLE001 - רושמים רק את סוג השגיאה
        return {
            "status": None,
            "headers": {},
            "body": b"",
            "truncated": False,
            "ms": round((time.monotonic() - t0) * 1000),
            "error": type(e).__name__,
        }


def parse_json(body):
    try:
        return json.loads(body.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None


def describe(obj):
    """מבנה התשובה בלבד: סוג, שמות שדות וכמויות. בלי ערכים."""
    if isinstance(obj, list):
        first = obj[0] if obj else None
        return {
            "type": "list",
            "count": len(obj),
            "item_keys": sorted(first.keys())[:40] if isinstance(first, dict) else [],
        }
    if isinstance(obj, dict):
        out = {"type": "object", "keys": sorted(obj.keys())[:40]}
        for k in LIST_KEYS:
            if isinstance(obj.get(k), list):
                inner = describe(obj[k])
                out["list_key"] = k
                out["count"] = inner["count"]
                out["item_keys"] = inner["item_keys"]
                break
        return out
    if obj is None:
        return {"type": "none"}
    return {"type": type(obj).__name__}


def _as_int(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and re.fullmatch(r"\d{1,7}", v.strip()):
        return int(v.strip())
    return None


def find_parcel(obj, depth=0):
    """מחפש גוש וחלקה בכל מקום בתשובה. מחזיר (gush, helka) או None."""
    if depth > 6:
        return None
    if isinstance(obj, dict):
        gush = helka = None
        for k, v in obj.items():
            lk = str(k).lower()
            if gush is None and "gush" in lk:
                gush = _as_int(v)
            elif helka is None and ("helka" in lk or lk == "parcel"):
                helka = _as_int(v)
        if gush and helka:
            return (gush, helka)
        for v in obj.values():
            hit = find_parcel(v, depth + 1)
            if hit:
                return hit
    elif isinstance(obj, list):
        for v in obj[:20]:
            hit = find_parcel(v, depth + 1)
            if hit:
                return hit
    return None


def trim(obj, depth=0):
    """דוגמה מקוצרת: עד 3 פריטים בכל רשימה ועד 300 תווים בכל מחרוזת. נתונים ציבוריים בלבד."""
    if depth > 12:
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


def limit_headers(headers):
    return {k: v for k, v in headers.items() if any(h in k for h in LIMIT_HINTS)}


def run_check(cid, label, path, params=None):
    res = fetch(path, params)
    data = parse_json(res["body"]) if res["status"] == 200 else None
    ok = res["status"] == 200 and data is not None
    check = {
        "id": cid,
        "label": label,
        "path": path,
        "ok": ok,
        "status": res["status"],
        "ms": res["ms"],
        "kb": round(len(res["body"]) / 1024, 1),
        "truncated": res["truncated"],
        "shape": describe(data) if ok else None,
        "error": res["error"] if not ok else None,
    }
    if not ok:
        print(f"[{cid}] failed: {res['error'] or 'BadJSON'}")
    time.sleep(PAUSE)
    return check, data, res["headers"]


def cors_verdict(headers):
    acao = headers.get("access-control-allow-origin")
    if acao in ("*", ORIGIN):
        return {"allow_origin": acao, "browser": "open"}
    if acao:
        return {"allow_origin": acao, "browser": "other_origin"}
    return {"allow_origin": None, "browser": "closed"}


def probe():
    checks, limits, samples = [], {}, {}
    cors = None

    def add(cid, label, path, params=None):
        nonlocal cors
        c, data, hdrs = run_check(cid, label, path, params)
        checks.append(c)
        limits.update(limit_headers(hdrs))
        if cors is None and c["ok"]:  # רק מתשובה תקינה: שגיאות לא מעידות על CORS
            cors = cors_verdict(hdrs)
        if c["ok"]:
            samples[cid] = trim(data)
        return c, data

    add("stats", "סטטיסטיקה כללית", "/api/nadlan/stats")
    add("streets", "חיפוש רחוב", "/api/nadlan/streets", {"q": "אבימ", "settlement": "7900"})
    _, addr = add("address", "כתובת לחלקה", "/api/nadlan/address",
                  {"city": "פתח תקווה", "street": "אבימלך", "number": "8"})
    add("lookup", "נקודה על המפה", "/api/nadlan/lookup",
        {"lat": "32.0853", "lon": "34.7818", "fields": "identity,zip,stat_area,deals"})

    parcel = find_parcel(addr) if addr is not None else None
    g, h = parcel or FALLBACK_PARCEL
    add("parcel", "פרטי חלקה", f"/api/nadlan/parcel/{g}/{h}")
    pd, _pd_data = add("parcel_deals", "עסקאות בחלקה", f"/api/nadlan/parcel/{g}/{h}/deals", {"limit": "5"})
    ex, _ = add("deals_example", "עסקאות בתת-חלקה", "/api/nadlan/parcel/7104/289/deals",
                {"sub_parcel": "118", "limit": "5"})

    deal_fields = []
    for c in (pd, ex):
        if c["ok"] and c["shape"] and c["shape"].get("item_keys"):
            deal_fields = c["shape"]["item_keys"]
            break

    ok_ms = [c["ms"] for c in checks if c["ok"]]
    deals_ok = any(c["ok"] and (c["shape"] or {}).get("count", 0) > 0 for c in (pd, ex))
    report = {
        "probe_version": PROBE_VERSION,
        "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "base": BASE,
        "origin_tested": ORIGIN,
        "checks": checks,
        "cors": cors or {"allow_origin": None, "browser": "unknown"},
        "limit_headers": limits,
        "parcel_from_address": {"gush": parcel[0], "helka": parcel[1]} if parcel else None,
        "deal_fields": deal_fields,
        "summary": {
            "ok": sum(1 for c in checks if c["ok"]),
            "total": len(checks),
            "median_ms": round(statistics.median(ok_ms)) if ok_ms else None,
            "deals_ok": deals_ok,
        },
    }
    return report, samples


def telegram_text(r):
    s = r["summary"]
    cors_txt = {
        "open": "✅ פתוח: בדוק נכס יעבוד ישירות מהטלפון",
        "closed": "❌ סגור: בדוק נכס ירוץ דרך GitHub Actions",
        "other_origin": "⚠️ פתוח לאתר אחר בלבד",
    }.get(r["cors"]["browser"], "❓ לא ידוע")
    lines = [
        "🏠 הנדלן של תומר: בדיקת היתכנות",
        f"נקודות קצה שענו: {s['ok']} מתוך {s['total']}",
        f"זמן תגובה חציוני: {s['median_ms']} ms" if s["median_ms"] else "זמן תגובה: אין נתון",
        f"שליפת עסקאות: {'✅ עובדת' if s['deals_ok'] else '❌ לא עבדה'}",
        f"גישה מהדפדפן (CORS): {cors_txt}",
    ]
    if r["limit_headers"]:
        lines.append("נמצאו כותרות מגבלת נפח ✔")
    failed = [c["label"] for c in r["checks"] if not c["ok"]]
    if failed:
        lines.append("נכשלו: " + ", ".join(failed))
    lines.append(APP_URL)
    return "\n".join(lines)


def send_telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        print("telegram: skipped (secrets not set)")
        return False
    if not re.fullmatch(r"-?\d{3,20}", chat):
        print("telegram: skipped (chat id format)")
        return False
    data = urllib.parse.urlencode(
        {"chat_id": chat, "text": text, "disable_web_page_preview": "true"}
    ).encode()
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data, method="POST"
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status == 200
    except Exception as e:  # noqa: BLE001
        print(f"telegram: failed ({type(e).__name__})")
        return False


def main():
    report, samples = probe()
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    with open(SAMPLES_OUT, "w", encoding="utf-8") as f:
        json.dump({"probe_version": PROBE_VERSION, "run_at": report["run_at"], "samples": samples},
                  f, ensure_ascii=False, indent=2)
    s = report["summary"]
    print(f"probe done: {s['ok']}/{s['total']} ok, cors={report['cors']['browser']}")
    send_telegram(telegram_text(report))


if __name__ == "__main__":
    main()
