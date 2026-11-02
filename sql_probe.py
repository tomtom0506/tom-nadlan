#!/usr/bin/env python3
"""tom-nadlan / sql_probe.py
בדיקת היתכנות לגיליון הראשי: נקודת הקצה של שאילתות SQL ב"גרסאות לעם".
קריאה בלבד, 7 שאילתות קטנות עם השהיה ביניהן. התוצאות נשמרות ב-data/sql_probe.json
וסיכום נשלח לטלגרם. בלוגים נרשם רק סוג השגיאה, וטוקנים לא מודפסים לעולם.
"""
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

PROBE_VERSION = "1.0.2"
DATASET_ID = "fd06f5ae-8a4f-4120-b275-8a514ad23499"
TABLE = "append_taxes_nadlan_full_f41fb496_fd06f5ae"
SQL_URL = f"https://www.over.org.il/api/append/{DATASET_ID}/sql"
ORIGIN = "https://tomtom0506.github.io"
APP_URL = "https://tomtom0506.github.io/tom-nadlan/"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "sql_probe.json")
UA = "tom-nadlan-probe/1.0 (personal, read-only)"
TIMEOUT = 45
PAUSE = 2.0
MAX_ROWS_SAVED = 40

APT = "deal_nature LIKE '%דיר%'"
AMT = "CAST(deal_amount AS REAL)"
QUERIES = [
    ("schema", "מבנה הטבלה", f"SELECT * FROM {TABLE} LIMIT 2"),
    ("count_2026", "ספירה של 2026", f"SELECT COUNT(*) AS n FROM {TABLE} WHERE deal_date LIKE '%/2026'"),
    ("days", "עסקאות לפי יום (אוק'-נוב')",
     f"SELECT deal_date, COUNT(*) AS n FROM {TABLE} "
     f"WHERE deal_date LIKE '%/10/2026' OR deal_date LIKE '%/11/2026' GROUP BY deal_date ORDER BY n DESC LIMIT 62"),
    ("natures", "סוגי עסקאות", f"SELECT deal_nature, COUNT(*) AS n FROM {TABLE} WHERE deal_date LIKE '%/2026' "
                               f"GROUP BY deal_nature ORDER BY n DESC LIMIT 30"),
    ("cities", "סיכום לפי עיר (ספטמבר)",
     f"SELECT settlement, COUNT(*) AS n, AVG({AMT}) AS avg_amt, MIN({AMT}) AS min_amt, MAX({AMT}) AS max_amt "
     f"FROM {TABLE} WHERE deal_date LIKE '%/09/2026' AND {APT} GROUP BY settlement ORDER BY n DESC LIMIT 15"),
    ("rooms", "פילוח חדרים", f"SELECT room_num, COUNT(*) AS n FROM {TABLE} WHERE deal_date LIKE '%/09/2026' AND {APT} "
                            f"GROUP BY room_num ORDER BY n DESC LIMIT 20"),
    ("portion", "חלקי בעלות", f"SELECT portion, COUNT(*) AS n FROM {TABLE} WHERE deal_date LIKE '%/09/2026' AND {APT} "
                             f"GROUP BY portion ORDER BY n DESC LIMIT 15"),
]


def safe_snippet(b):
    t = b.decode("utf-8", "replace") if isinstance(b, bytes) else str(b)
    t = re.sub(r"\s+", " ", t)
    return t[:300]


def post_sql(sql):
    data = json.dumps({"sql": sql}).encode("utf-8")
    req = urllib.request.Request(SQL_URL, data=data, method="POST", headers={
        "User-Agent": UA, "Content-Type": "application/json", "Accept": "application/json", "Origin": ORIGIN})
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            body = r.read(5_000_000)
            return r.status, {k.lower(): v for k, v in r.headers.items()}, body, round((time.monotonic() - t0) * 1000), None
    except urllib.error.HTTPError as e:
        body = b""
        try:
            body = e.read(2000)
        except Exception:  # noqa: BLE001
            pass
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, body, round((time.monotonic() - t0) * 1000), "HTTPError"
    except Exception as e:  # noqa: BLE001
        return None, {}, b"", round((time.monotonic() - t0) * 1000), type(e).__name__


def run_query(qid, label, sql):
    status, headers, body, ms, err = post_sql(sql)
    if status == 429:
        ra = headers.get("retry-after", "")
        time.sleep(min(int(ra) + 2, 60) if ra.isdigit() else 35)
        status, headers, body, ms, err = post_sql(sql)
    res = {"id": qid, "label": label, "sql": sql, "ok": False, "status": status, "ms": ms,
           "kb": round(len(body) / 1024, 1), "error": err, "columns": [], "row_count": 0, "rows": []}
    if status == 200:
        try:
            data = json.loads(body.decode("utf-8"))
            rows = data.get("rows") if isinstance(data, dict) else None
            if isinstance(rows, list):
                res["ok"] = True
                res["row_count"] = len(rows)
                res["rows"] = rows[:MAX_ROWS_SAVED]
                if rows and isinstance(rows[0], dict):
                    res["columns"] = list(rows[0].keys())
                res["response_keys"] = sorted(data.keys())[:20]
            else:
                res["error"] = "NoRows"
                res["body_snippet"] = safe_snippet(body)
        except Exception:  # noqa: BLE001
            res["error"] = "BadJSON"
            res["body_snippet"] = safe_snippet(body)
    elif body:
        res["body_snippet"] = safe_snippet(body)
    if not res["ok"]:
        print(f"[{qid}] failed: {res['error'] or status}")
    time.sleep(PAUSE)
    return res, headers


def cors_preflight():
    req = urllib.request.Request(SQL_URL, method="OPTIONS", headers={
        "User-Agent": UA, "Origin": ORIGIN, "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            h = {k.lower(): v for k, v in r.headers.items()}
            return {"status": r.status, "allow_origin": h.get("access-control-allow-origin"),
                    "allow_methods": h.get("access-control-allow-methods"), "allow_headers": h.get("access-control-allow-headers")}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "allow_origin": None}
    except Exception as e:  # noqa: BLE001
        return {"status": None, "error": type(e).__name__}


def latest_day(rows):
    best = None
    for r in rows or []:
        d = str(r.get("deal_date", ""))
        m = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", d)
        if m:
            iso = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
            if best is None or iso > best:
                best = iso
    return best


def probe():
    results, limits = [], {}
    for qid, label, sql in QUERIES:
        r, h = run_query(qid, label, sql)
        results.append(r)
        limits.update({k: v for k, v in h.items() if any(x in k for x in ("ratelimit", "rate-limit", "retry-after", "budget", "quota"))})
    by = {r["id"]: r for r in results}
    summary = {
        "ok": sum(1 for r in results if r["ok"]),
        "total": len(results),
        "group_by_ok": by["days"]["ok"] and by["days"]["row_count"] > 0,
        "city_summary_ok": by["cities"]["ok"] and by["cities"]["row_count"] > 0,
        "latest_deal_day": latest_day(by["days"]["rows"]) if by["days"]["ok"] else None,
        "columns": by["schema"]["columns"],
    }
    return {
        "probe_version": PROBE_VERSION,
        "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sql_url": SQL_URL,
        "table": TABLE,
        "queries": results,
        "cors_preflight": cors_preflight(),
        "limit_headers": limits,
        "summary": summary,
    }


def telegram_text(r):
    s = r["summary"]
    c = r["cors_preflight"]
    lines = [
        "🏠 הנדלן של תומר: בדיקת SQL לגיליון הראשי",
        f"שאילתות שעברו: {s['ok']} מתוך {s['total']}",
        f"סיכומים (GROUP BY): {'✅ עובד' if s['group_by_ok'] else '❌ לא עבד'}",
        f"סיכום לפי עיר: {'✅ עובד' if s['city_summary_ok'] else '❌ לא עבד'}",
        f"היום האחרון עם עסקאות: {s['latest_deal_day'] or 'לא ידוע'}",
        f"גישה מהדפדפן (CORS): {'✅' if c.get('allow_origin') in ('*', ORIGIN) else '❌'}",
    ]
    failed = [q["label"] for q in r["queries"] if not q["ok"]]
    if failed:
        lines.append("נכשלו: " + ", ".join(failed))
    lines.append(APP_URL)
    return "\n".join(lines)


def send_telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat or not re.fullmatch(r"-?\d{3,20}", chat):
        print("telegram: skipped")
        return False
    data = urllib.parse.urlencode({"chat_id": chat, "text": text, "disable_web_page_preview": "true"}).encode()
    try:
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data, method="POST")
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status == 200
    except Exception as e:  # noqa: BLE001
        print(f"telegram: failed ({type(e).__name__})")
        return False


def main():
    report = probe()
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    s = report["summary"]
    print(f"sql probe: {s['ok']}/{s['total']} ok, group_by={s['group_by_ok']}, latest={s['latest_deal_day']}")
    send_telegram(telegram_text(report))


if __name__ == "__main__":
    main()
