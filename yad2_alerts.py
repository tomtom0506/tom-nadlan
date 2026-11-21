#!/usr/bin/env python3
"""tom-nadlan / yad2_alerts.py
מודעות חדשות מהתראות החיפוש של יד2 (ומדלן), מול עסקאות דומות, וההזדמנויות לטלגרם.

איך זה עובד:
- תומר נרשם להתראות חיפוש ביד2. מסנן ב-Gmail מעביר אותן לתיבה ייעודית.
- הסקריפט קורא את התיבה ב-IMAP (כתובת וסיסמת אפליקציה רק מ-GitHub Secrets), ומחלץ את המודעות מגוף המייל.
  אין גישה לאתר של יד2 ואין סריקה: רק מה שהגיע במייל שתומר נרשם אליו.
- לכל מודעה: העיר מזוהה לפי הרחוב (עסקאות GovMap דרך over.org.il), ומושווית לעסקאות דומות מ-24 החודשים האחרונים:
  אותו סוג נכס, חדרים ±1, שטח ±20% (בית פרטי ±30%). קודם באותו רחוב, ואם אין מספיק, בכל העיר.
- לטלגרם נשלחות רק דירות (לא בתים פרטיים) שזולות ב-BELOW_PCT% לפחות מהחציון למ"ר. עם SEND_ALL=1 נשלחות כל המודעות (לבדיקת החילוץ).
- מייל שטופל מקבל תווית ב-Gmail ולא נקרא שוב. שום דבר לא נשמר ב-repo, ובלוג (ציבורי) נרשמים רק מספרים.
"""
import email
import html
import imaplib
import os
import re
import statistics
import sys
from datetime import date, timedelta
from email.utils import parseaddr
from html.parser import HTMLParser
from urllib.parse import urlparse

import deals_api as api
from weekly_watch import send_telegram

YAD2_VERSION = "1.8.0"
IMAP_HOST = "imap.gmail.com"
DONE_LABEL = "tom-nadlan-done"
SENDER_DOMAINS = ("yad2.co.il", "madlan.co.il")
LINK_DOMAINS = ("yad2.co.il", "madlan.co.il")
MAX_MAILS = 20
LOOKBACK_DAYS = 14
BELOW_PCT = 15
MAX_BELOW_PCT = 50          # זול מזה כנראה טעות במודעה (מחיר חסר ספרה, חלק מנכס, השכרה)
MIN_COMPS = 5
COMPS_MONTHS = 24
CITY_PAGES = 5              # עד 1,000 עסקאות לעיר
TIME_BUDGET = 10 * 60
MSG_MAX = 3800

# אזורי החיפוש של יד2 -> ערים. העיר עצמה לא מופיעה בכל מודעה, רק השכונה והרחוב.
AREA_CITIES = {
    "בקעת אונו": ["קריית אונו", "אור יהודה", "יהוד-מונוסון", "גני תקווה", "סביון", "גבעת שמואל", "כפר אזר"],
    "רמת גן גבעתיים": ["רמת גן", "גבעתיים"],
    "תל אביב": ["תל אביב -יפו", "תל אביב-יפו"],
    "פתח תקווה": ["פתח תקווה"],
    "בת ים חולון": ["בת ים", "חולון"],
    "ראשון לציון": ["ראשון לציון"],
    "לוד רמלה": ["לוד", "רמלה"],
}
FALLBACK_CITIES = ["פתח תקווה", "קריית אונו", "רמת גן", "גבעתיים", "בני ברק", "תל אביב -יפו", "חולון", "בת ים",
                   "ראשון לציון", "אור יהודה", "יהוד-מונוסון", "גני תקווה", "לוד", "רמלה", "ראש העין", "הוד השרון",
                   "כפר סבא", "רעננה", "הרצלייה", "נתניה", "רחובות", "נס ציונה", "יבנה", "אשדוד", "חיפה", "באר שבע",
                   "אשקלון", "חדרה", "קריית גת", "מודיעין-מכבים-רעות", "ירושלים"]

APT_NATURES = ("דירה בבית קומות", "דירת גן", "דירת גג", "דירה", "דירה בבית קומות - דירה בקוטג'")
HOUSE_NATURES = ("קוטג' חד משפחתי", "קוטג' דו משפחתי", "קוטג' טורי", "בית בודד", "וילה")
HOUSE_WORDS = ("בית פרטי", "קוטג", "דו משפחתי", "דו-משפחתי", "וילה", "טורי")


# ---------- מייל ----------

class _Text(HTMLParser):
    """טקסט מ-HTML עם שבירת שורות, ורשימת הקישורים לפי הסדר (טקסט הקישור + href)."""
    BREAK = {"br", "p", "div", "tr", "td", "li", "h1", "h2", "h3", "h4", "table", "section"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.links, self._a, self._skip = [], [], None, 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        if tag in self.BREAK:
            self.parts.append("\n")
        if tag == "a":
            self._a = [dict(attrs).get("href") or "", ""]

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        if tag in self.BREAK:
            self.parts.append("\n")
        if tag == "a" and self._a is not None:
            self.links.append((self._a[1].strip(), self._a[0]))
            self._a = None

    def handle_data(self, data):
        if self._skip:
            return
        self.parts.append(data)
        if self._a is not None:
            self._a[1] += data


def html_to_lines(raw):
    p = _Text()
    p.feed(raw)
    text = "".join(p.parts).replace(" ", " ")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()]
    return [ln for ln in lines if ln], p.links


def mail_body(msg):
    """(שורות, קישורים) מתוך חלק ה-HTML, ואם אין, מהטקסט."""
    html_part = text_part = None
    for part in msg.walk():
        ct = part.get_content_type()
        if ct == "text/html" and html_part is None:
            html_part = part
        elif ct == "text/plain" and text_part is None:
            text_part = part
    part = html_part or text_part
    if part is None:
        return [], []
    raw = part.get_payload(decode=True) or b""
    raw = raw.decode(part.get_content_charset() or "utf-8", "replace")
    if part is html_part:
        return html_to_lines(raw)
    links = [("", u) for u in re.findall(r"https?://\S+", raw)]
    return [ln.strip() for ln in raw.splitlines() if ln.strip()], links


def sender_ok(msg):
    addr = parseaddr(str(msg.get("From") or ""))[1].lower()
    dom = addr.rsplit("@", 1)[-1]
    return any(dom == d or dom.endswith("." + d) for d in SENDER_DOMAINS)


# ---------- חילוץ מודעות ----------

PRICE = re.compile(r"(?:₪\s*([\d,]{5,12})|([\d,]{5,12})\s*₪)")
ROOMS = re.compile(r"([\d.]+)\s*חד")
SQM = re.compile(r"([\d,.]+)\s*מ[\"״'’]{1,2}ר")
STREET = re.compile(r"^(.*?\D)\s*(\d+[א-ת]?)?$")


def _n(s):
    try:
        return float(str(s).replace(",", ""))
    except ValueError:
        return None


def parse_ads(lines, links=()):
    """מודעות מתוך שורות המייל. כל מודעה: שורת מחיר, שורת כתובת, ושורת פרטים 'שכונה | סוג | N חדרים | M מ"ר'.
    הכותרת 'דירות למכירה ב...' נותנת את אזור החיפוש."""
    area = None
    for ln in lines[:40]:
        m = re.search(r"למכירה ב(.+)$", ln)
        if m:
            area = m.group(1).strip()
            break
    ads, i = [], 0
    while i < len(lines):
        m = PRICE.search(lines[i])
        if not m or re.search(r"מינימלי|מקסימלי", lines[i]):
            i += 1
            continue
        price = _n(m.group(1) or m.group(2))
        addr, details, j = None, None, i + 1
        while j < min(len(lines), i + 6):
            ln = lines[j]
            if PRICE.search(ln):
                break
            if "|" in ln or ROOMS.search(ln) or SQM.search(ln):
                details = (details + " | " + ln) if details else ln
            elif addr is None and not re.search(r"לפרטים|לצפייה", ln):
                addr = ln
            j += 1
        if price and details:
            parts = [p.strip() for p in details.split("|") if p.strip()]
            rooms = next((_n(ROOMS.search(p).group(1)) for p in parts if ROOMS.search(p)), None)
            sqm = next((_n(SQM.search(p).group(1)) for p in parts if SQM.search(p)), None)
            rest = [p for p in parts if not ROOMS.search(p) and not SQM.search(p)]
            street, house = None, None
            if addr:
                sm = STREET.match(addr)
                street, house = (sm.group(1).strip(), sm.group(2)) if sm else (addr, None)
            ads.append({"price": int(price), "street": street, "house": house,
                        "neighborhood": rest[0] if rest else None, "kind": rest[1] if len(rest) > 1 else None,
                        "rooms": rooms, "sqm": sqm, "area": area, "link": None})
        i = j
    detail_links = [u for t, u in links if re.search(r"לפרטים|לצפייה", t or "")]
    for ad, u in zip(ads, detail_links):
        ad["link"] = safe_link(u)
    return ads


def safe_link(u):
    try:
        p = urlparse(u or "")
    except ValueError:
        return None
    host = (p.hostname or "").lower()
    if p.scheme == "https" and any(host == d or host.endswith("." + d) for d in LINK_DOMAINS):
        return u
    return None


def is_house(kind):
    return bool(kind) and any(w in kind for w in HOUSE_WORDS)


# ---------- השוואה ----------

def govmap(params):
    data = api.get_json("/api/deals/govmap/search", params)
    rows = data.get("data")
    if not isinstance(rows, list):
        raise api.ApiError("NoData")
    return rows, data.get("total") if isinstance(data.get("total"), int) else None


def find_city(street, area, cache):
    """העיר שבה יש עסקאות ברחוב הזה. קודם ערי אזור החיפוש, אחר כך רשימה רחבה."""
    if not street:
        return None
    key = (street, area)
    if key in cache:
        return cache[key]
    cands = AREA_CITIES.get(area or "", []) + [c for c in FALLBACK_CITIES if c not in AREA_CITIES.get(area or "", [])]
    found = None
    for c in cands:
        _, total = govmap({"settlement": c, "street": street, "limit": 1})
        if total:
            found = c
            break
    cache[key] = found
    return found


def comparable(ad, d, since):
    day = str(d.get("deal_date") or "")[:10]
    if day < since:
        return None
    nature = d.get("deal_nature")
    if nature not in (HOUSE_NATURES if is_house(ad["kind"]) else APT_NATURES):
        return None
    amt, area, rooms = _n(d.get("deal_amount")), _n(d.get("asset_area")), _n(d.get("rooms"))
    if not amt or not area or area <= 0 or amt < 150_000:
        return None
    tol = 0.3 if is_house(ad["kind"]) else 0.2
    if ad["sqm"] and abs(area - ad["sqm"]) > ad["sqm"] * tol:
        return None
    if ad["rooms"] and not is_house(ad["kind"]) and (rooms is None or abs(rooms - ad["rooms"]) > 1):
        return None  # בבתים פרטיים מספר החדרים בעסקאות חסר או לא אחיד: משווים לפי שטח בלבד
    return amt / area


def comps_for(ad, city, today):
    since = (today - timedelta(days=int(COMPS_MONTHS * 30.5))).isoformat()
    rows, _ = govmap({"settlement": city, "street": ad["street"], "date_from": since, "limit": api.PAGE})
    vals = [v for v in (comparable(ad, d, since) for d in rows) if v]
    if len(vals) >= MIN_COMPS:
        return vals, "street"
    base = {"settlement": city, "date_from": since, "limit": api.PAGE, "sort": "date_desc"}
    if ad["rooms"] and not is_house(ad["kind"]):
        base.update(min_rooms=max(0, ad["rooms"] - 1), max_rooms=ad["rooms"] + 1)
    vals = []
    for page in range(CITY_PAGES):
        rows, total = govmap(dict(base, offset=page * api.PAGE))
        vals += [v for v in (comparable(ad, d, since) for d in rows) if v]
        if len(rows) < api.PAGE or (total is not None and (page + 1) * api.PAGE >= total):
            break
    return vals, "city"


def evaluate(ad, today, city_cache):
    """מוסיף למודעה: city, n_comps, scope, median_ppm, ppm, pct, target. מחזיר את המודעה."""
    ad.update(city=None, n_comps=0, scope=None, median_ppm=None, ppm=None, pct=None, target=None)
    if not ad["sqm"] or ad["sqm"] <= 0:
        return ad
    ad["ppm"] = ad["price"] / ad["sqm"]
    ad["city"] = find_city(ad["street"], ad["area"], city_cache)
    if not ad["city"]:
        return ad
    vals, scope = comps_for(ad, ad["city"], today)
    ad["n_comps"], ad["scope"] = len(vals), scope
    if len(vals) >= MIN_COMPS:
        med = statistics.median(vals)
        ad["median_ppm"] = round(med)
        ad["pct"] = round((ad["ppm"] / med - 1) * 100)
        ad["target"] = round(statistics.quantiles(vals, n=4)[0] * ad["sqm"], -3)  # רבעון תחתון של הדומות
    return ad


def is_bargain(ad):
    """בית פרטי לא נשלח כהזדמנות: במודעות השטח הוא לפעמים של המגרש ולפעמים של הבנייה, וההשוואה גסה מדי."""
    return ad["pct"] is not None and not is_house(ad["kind"]) and -MAX_BELOW_PCT <= ad["pct"] <= -BELOW_PCT


# ---------- טלגרם ----------

LRI, PDI = "⁦", "⁩"


def esc(s):
    return html.escape(str(s or ""), quote=False)


def nis(v):
    return f"{LRI}₪{round(v):,}{PDI}"


def ad_line(ad):
    head = f"<b>{esc(ad['street'] or '')} {esc(ad['house'] or '')}</b>, {esc(ad['city'] or ad['area'] or '')}".strip()
    spec = " · ".join(x for x in [esc(ad["kind"]), f"{ad['rooms']:g} חד׳" if ad["rooms"] else "",
                                  f"{ad['sqm']:g} מ\"ר" if ad["sqm"] else ""] if x)
    line = f"{head}\n{nis(ad['price'])} · {spec}"
    if ad["pct"] is not None:
        where = "באותו רחוב" if ad["scope"] == "street" else "בעיר"
        line += (f"\n{LRI}{ad['pct']:+d}%{PDI} מול {ad['n_comps']} עסקאות דומות {where} "
                 f"(חציון {nis(ad['median_ppm'])} למ\"ר)")
        if is_house(ad["kind"]):
            line += "\n⚠️ בית פרטי: הערכה גסה, כי השטח במודעה עשוי להיות של המגרש ולא של הבית"
        elif ad["target"]:
            line += f"\nמחיר יעד להצעה: {nis(ad['target'])}"
    else:
        why = "העיר לא זוהתה" if not ad["city"] else f"רק {ad['n_comps']} עסקאות דומות"
        line += f"\nאין הערכה: {why}"
    if ad["link"]:
        line += f'\n<a href="{html.escape(ad["link"], quote=True)}">למודעה</a>'
    return line


def build_messages(ads, send_all):
    picked = ads if send_all else [a for a in ads if is_bargain(a)]
    if not picked:
        return []
    title = (f"🔎 בדיקת חילוץ: {len(ads)} מודעות מהתראות יד2" if send_all
             else f"💎 {len(picked)} מודעות חדשות {BELOW_PCT}%+ מתחת לעסקאות דומות")
    msgs, cur = [], f"<b>{title}</b>"
    for ad in sorted(picked, key=lambda a: (a["pct"] is None, a["pct"] or 0)):
        line = ad_line(ad)
        if len(cur) + len(line) + 2 > MSG_MAX:
            msgs.append(cur)
            cur = ""
        cur += ("\n\n" if cur else "") + line
    msgs.append(cur)
    return msgs


# ---------- IMAP ----------

def fetch_new_mails(conn, max_mails=MAX_MAILS):
    """מיילים מיד2/מדלן מ-LOOKBACK_DAYS הימים האחרונים שעוד אין להם התווית DONE_LABEL. מחזיר [(uid, Message)]."""
    conn.select("INBOX")
    q = f'newer_than:{LOOKBACK_DAYS}d -label:{DONE_LABEL} ({" OR ".join("from:" + d for d in SENDER_DOMAINS)})'
    typ, data = conn.uid("SEARCH", "X-GM-RAW", f'"{q}"')
    if typ != "OK":
        raise RuntimeError("imap search failed")
    uids = (data[0] or b"").split()[-max_mails:]
    out = []
    for uid in uids:
        typ, parts = conn.uid("FETCH", uid, "(BODY.PEEK[])")
        if typ != "OK":
            continue
        raw = next((p[1] for p in parts if isinstance(p, tuple)), None)
        if raw:
            out.append((uid, email.message_from_bytes(raw)))
    return out


def mark_done(conn, uid):
    conn.uid("STORE", uid, "+X-GM-LABELS", f"({DONE_LABEL})")


def run(conn, today=None, send_all=False, send=send_telegram):
    today = today or date.today()
    mails = fetch_new_mails(conn)
    ads, done, skipped = [], [], 0
    for uid, msg in mails:
        if not sender_ok(msg):
            skipped += 1
            done.append(uid)
            continue
        lines, links = mail_body(msg)
        ads += parse_ads(lines, links)
        done.append(uid)
    seen, uniq = set(), []
    for ad in ads:
        k = (ad["street"], ad["house"], ad["price"], ad["sqm"])
        if k not in seen:
            seen.add(k)
            uniq.append(ad)
    city_cache, errors = {}, 0
    for ad in uniq:
        try:
            evaluate(ad, today, city_cache)
        except api.ApiError:
            errors += 1
    msgs = build_messages(uniq, send_all)
    ok = all(send(m, html_mode=True) for m in msgs)
    if ok:  # רק אחרי שליחה מוצלחת, כדי שמייל לא ילך לאיבוד
        for uid in done:
            mark_done(conn, uid)
    print(f"yad2: mails={len(mails)} skipped={skipped} ads={len(uniq)} evaluated={sum(a['pct'] is not None for a in uniq)} "
          f"bargains={sum(is_bargain(a) for a in uniq)} messages={len(msgs)} api_errors={errors} sent={'yes' if ok else 'no'}")
    return 0 if ok else 1


def main():
    user = os.environ.get("YAD2_IMAP_USER", "").strip()
    pwd = os.environ.get("YAD2_IMAP_PASSWORD", "").replace(" ", "").strip()
    if not user or not pwd:
        print("yad2: skipped (secrets)")
        return 1
    api.start_clock(TIME_BUDGET)
    conn = imaplib.IMAP4_SSL(IMAP_HOST)
    try:
        try:
            conn.login(user, pwd)
        except imaplib.IMAP4.error:
            print("yad2: login failed")  # בלי פרטים: לא מדפיסים משתמש או סיסמה
            return 1
        return run(conn, send_all=os.environ.get("SEND_ALL") == "1")
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    sys.exit(main())
