"""בדיקות offline ל-yad2_alerts.py (v1.8.0). הרצה: python3 -m unittest discover -s tests"""
import email
import os
import sys
import unittest
from datetime import date
from email.mime.text import MIMEText
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import yad2_alerts as y  # noqa: E402

MAIL_HTML = """<html><body><div>היי תומר,</div><div>מצאנו 2 מודעות חדשות שמתאימות לחיפוש שלך:</div>
<div><b>דירות למכירה בבקעת אונו</b></div><div>מחוז: מרכז והשרון | מחיר מינימלי: 900000 | מחיר מקסימלי: 5000000</div>
<table><tr><td><img src="x"></td></tr><tr><td><b>₪ 3,140,000</b></td></tr><tr><td>יעקב בר סימנטוב 1</td></tr>
<tr><td>גבעת אוויה | בית פרטי/ קוטג' | 3 חדרים | 260 מ״ר</td></tr>
<tr><td><a href="https://www.yad2.co.il/item/abc">לפרטים נוספים</a></td></tr></table>
<table><tr><td><b>3,850,000 ₪</b></td></tr><tr><td>מניה שוחט 6</td></tr>
<tr><td>נווה איילון | דירה | 5 חדרים | 123 מ"ר</td></tr>
<tr><td><a href="https://evil.example/x">לפרטים נוספים</a></td></tr></table>
<script>alert(1)</script></body></html>"""


def mail(html_body, sender="Yad2 <alerts@mail.yad2.co.il>"):
    m = MIMEText(html_body, "html", "utf-8")
    m["From"] = sender
    m["Subject"] = "מודעות חדשות"
    return email.message_from_bytes(m.as_bytes())


class ParseTests(unittest.TestCase):
    def ads(self):
        return y.parse_ads(*y.mail_body(mail(MAIL_HTML)))

    def test_parses_both_ads(self):
        a, b = self.ads()
        self.assertEqual((a["price"], a["street"], a["house"], a["rooms"], a["sqm"], a["area"]),
                         (3_140_000, "יעקב בר סימנטוב", "1", 3, 260, "בקעת אונו"))
        self.assertEqual((b["price"], b["street"], b["house"], b["kind"], b["rooms"], b["sqm"]),
                         (3_850_000, "מניה שוחט", "6", "דירה", 5, 123))
        self.assertEqual(a["neighborhood"], "גבעת אוויה")

    def test_price_range_header_is_not_an_ad(self):
        self.assertEqual(len(self.ads()), 2)

    def test_only_yad2_links_kept(self):
        a, b = self.ads()
        self.assertEqual(a["link"], "https://www.yad2.co.il/item/abc")
        self.assertIsNone(b["link"])
        self.assertIsNone(y.safe_link("http://www.yad2.co.il/x"))          # לא https
        self.assertIsNone(y.safe_link("https://yad2.co.il.evil.com/x"))   # דומיין מתחזה

    def test_script_ignored(self):
        lines, _ = y.mail_body(mail(MAIL_HTML))
        self.assertFalse(any("alert(1)" in ln for ln in lines))

    def test_sender_check(self):
        self.assertTrue(y.sender_ok(mail(MAIL_HTML)))
        self.assertTrue(y.sender_ok(mail(MAIL_HTML, "x@madlan.co.il")))
        self.assertFalse(y.sender_ok(mail(MAIL_HTML, "x@yad2.co.il.evil.com")))
        self.assertFalse(y.sender_ok(mail(MAIL_HTML, "friend@gmail.com")))


def deal(day, amount, area, rooms, nature="דירה בבית קומות"):
    return {"deal_date": day, "deal_amount": amount, "asset_area": area, "rooms": rooms, "deal_nature": nature}


class EvaluateTests(unittest.TestCase):
    AD = {"price": 2_400_000, "street": "הרצל", "house": "5", "neighborhood": "מרכז", "kind": "דירה",
          "rooms": 4, "sqm": 100, "area": "בקעת אונו", "link": None}

    def fake_govmap(self, street_deals, city_deals=()):
        def f(params):
            if params.get("limit") == 1:
                return ([{}], 1) if params["settlement"] == "אור יהודה" else ([], 0)
            if params.get("street"):
                return list(street_deals), len(street_deals)
            return list(city_deals), len(city_deals)
        return f

    def test_street_comps_and_bargain(self):
        street = [deal("2026-01-10", 3_000_000 + i * 10_000, 100, 4) for i in range(6)]
        with mock.patch.object(y, "govmap", side_effect=self.fake_govmap(street)):
            ad = y.evaluate(dict(self.AD), date(2026, 11, 21), {})
        self.assertEqual((ad["city"], ad["scope"], ad["n_comps"]), ("אור יהודה", "street", 6))
        self.assertEqual(ad["pct"], -21)
        self.assertTrue(y.is_bargain(ad))

    def test_falls_back_to_city_and_filters(self):
        street = [deal("2026-01-10", 3_000_000, 100, 4)]
        city = [deal("2026-01-10", 3_000_000, 100, 4)] * 5 + [
            deal("2026-01-10", 3_000_000, 200, 4),                 # שטח רחוק
            deal("2026-01-10", 3_000_000, 100, 7),                 # חדרים רחוקים
            deal("2020-01-10", 1_000_000, 100, 4),                 # ישן
            deal("2026-01-10", 3_000_000, 100, 4, "קרקע למגורים")]  # לא דירה
        with mock.patch.object(y, "govmap", side_effect=self.fake_govmap(street, city)):
            ad = y.evaluate(dict(self.AD), date(2026, 11, 21), {})
        self.assertEqual((ad["scope"], ad["n_comps"], ad["pct"]), ("city", 5, -20))

    def test_unknown_city_no_estimate(self):
        with mock.patch.object(y, "govmap", return_value=([], 0)):
            ad = y.evaluate(dict(self.AD, street="רחוב שאין"), date(2026, 11, 21), {})
        self.assertIsNone(ad["city"])
        self.assertFalse(y.is_bargain(ad))

    def test_house_never_bargain_and_ignores_rooms(self):
        houses = [deal("2026-01-10", 6_000_000, 250, None, "קוטג' חד משפחתי") for _ in range(5)]
        ad0 = dict(self.AD, kind="בית פרטי/ קוטג'", rooms=3, sqm=260, price=3_140_000)
        with mock.patch.object(y, "govmap", side_effect=self.fake_govmap(houses)):
            ad = y.evaluate(ad0, date(2026, 11, 21), {})
        self.assertEqual(ad["n_comps"], 5)
        self.assertLess(ad["pct"], -15)
        self.assertFalse(y.is_bargain(ad))

    def test_too_cheap_is_suspicious_not_bargain(self):
        ad = dict(self.AD, pct=-60, kind="דירה")
        self.assertFalse(y.is_bargain(ad))


class MessageTests(unittest.TestCase):
    def ad(self, **kw):
        base = {"price": 2_400_000, "street": "הרצל <b>", "house": "5", "city": "אור יהודה", "area": None,
                "kind": "דירה", "rooms": 4, "sqm": 100, "pct": -20, "n_comps": 6, "scope": "street",
                "median_ppm": 30000, "target": 2_300_000, "link": "https://www.yad2.co.il/item/a?x=1&y=2"}
        base.update(kw)
        return base

    def test_only_bargains_unless_send_all(self):
        ads = [self.ad(), self.ad(pct=5)]
        self.assertEqual(len(y.build_messages(ads, False)), 1)
        self.assertIn("1 מודעות חדשות", y.build_messages(ads, False)[0])
        self.assertIn("2 מודעות", y.build_messages(ads, True)[0])
        self.assertEqual(y.build_messages([self.ad(pct=5)], False), [])

    def test_escaping(self):
        m = y.build_messages([self.ad()], False)[0]
        self.assertIn("הרצל &lt;b&gt;", m)
        self.assertIn('href="https://www.yad2.co.il/item/a?x=1&amp;y=2"', m)

    def test_long_lists_split(self):
        msgs = y.build_messages([self.ad(street=f"רחוב {i}") for i in range(60)], False)
        self.assertGreater(len(msgs), 1)
        self.assertTrue(all(len(m) <= y.MSG_MAX for m in msgs))


class FakeImap:
    def __init__(self, mails):
        self.mails, self.labels = mails, {}

    def select(self, box):
        return "OK", [b"1"]

    def uid(self, cmd, *args):
        if cmd == "SEARCH":
            self.query = args[1]
            return "OK", [b" ".join(self.mails)]
        if cmd == "FETCH":
            return "OK", [(b"x", self.mails[args[0]].as_bytes())]
        if cmd == "STORE":
            self.labels[args[0]] = args[2]
            return "OK", []


class RunTests(unittest.TestCase):
    def test_labels_only_after_successful_send(self):
        conn = FakeImap({b"1": mail(MAIL_HTML), b"2": mail(MAIL_HTML, "spam@gmail.com")})
        def fake_eval(ad, *a):
            ad.update(city="x", pct=-20, n_comps=6, scope="street", median_ppm=1, target=1)
            return ad
        with mock.patch.object(y, "evaluate", side_effect=fake_eval):
            sent = []
            rc = y.run(conn, today=date(2026, 11, 21), send=lambda m, html_mode: sent.append(m) or False)
            self.assertEqual((rc, conn.labels), (1, {}))
            rc = y.run(conn, today=date(2026, 11, 21), send=lambda m, html_mode: True)
        self.assertEqual(rc, 0)
        self.assertEqual(set(conn.labels), {b"1", b"2"})
        self.assertIn("-label:tom-nadlan-done", conn.query)
        self.assertIn("from:yad2.co.il", conn.query)

    def test_no_secrets_exits_1(self):
        with mock.patch.dict(os.environ, {"YAD2_IMAP_USER": "", "YAD2_IMAP_PASSWORD": ""}):
            self.assertEqual(y.main(), 1)


if __name__ == "__main__":
    unittest.main()
