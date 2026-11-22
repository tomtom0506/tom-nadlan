"""בדיקות offline ללשונית "מציאות" (v1.7.0): ניקוי עסקאות GovMap, זיהוי מציאות, רחובות, מטמון, מכרזים, התראה ו-CSP.
הרצה: python3 -m unittest discover -s tests
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import date
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import build_bargains as bb  # noqa: E402
import csp_hashes  # noqa: E402
import deals_api as api  # noqa: E402
import notify_bargains as nb  # noqa: E402

TODAY = date(2026, 11, 21)


def raw(i, day="2026-10-01", amount=1_200_000, area=80.0, rooms=3.0, street="הרצל", ptype="דירה",
        nature="דירה בבית קומות"):
    return {"deal_id": 1000 + i, "deal_date": day, "deal_amount": amount, "asset_area": area, "rooms": rooms,
            "street": street, "house_num": str(i), "floor": "שנייה", "gush": 6000, "parcel": i,
            "property_type": ptype, "deal_nature": nature}


def cfg(**kw):
    c = {"budget": 1_500_000, "cities": ["עיר"], "recent_days": 60, "comps_months": 12, "below_pct": 15,
         "max_below_pct": 45, "min_comps": 5, "max_flags_per_city": 8, "streets_per_city": 8, "min_street_deals": 5}
    c.update(kw)
    return c


class CleanDealTests(unittest.TestCase):
    def test_valid_apartment(self):
        x = bb.clean_deal(raw(1), TODAY)
        self.assertEqual((x["amount"], x["area"], x["rooms"], x["street"]), (1_200_000, 80.0, 3.0, "הרצל"))

    def test_future_date_rejected(self):
        # במקור יש עסקאות עם תאריך עתידי (נמצאו 2027-04-25 בפתח תקווה)
        self.assertIsNone(bb.clean_deal(raw(1, day="2027-04-25"), TODAY))

    def test_non_apartment_rejected(self):
        self.assertIsNone(bb.clean_deal(raw(1, ptype="בנין", nature="משרד"), TODAY))
        self.assertIsNone(bb.clean_deal(raw(1, ptype="קרקע", nature="ללא תיכנון"), TODAY))

    def test_untyped_kept_if_plausible(self):
        self.assertIsNotNone(bb.clean_deal(raw(1, ptype=None, nature=None), TODAY))

    def test_implausible_area_per_room(self):
        self.assertIsNone(bb.clean_deal(raw(1, area=200, rooms=2), TODAY))   # 100 מ"ר לחדר
        self.assertIsNone(bb.clean_deal(raw(1, area=30, rooms=5), TODAY))    # 6 מ"ר לחדר


class NamesTests(unittest.TestCase):
    def test_spelling_variants(self):
        names = ["תל אביב-יפו", "תל אביב -יפו", "קרית אונו", "קריית אונו", "אונו"]
        self.assertEqual(bb.govmap_names(names, "תל אביב-יפו"), ["תל אביב -יפו", "תל אביב-יפו"])
        self.assertEqual(bb.govmap_names(names, "קריית אונו"), ["קריית אונו", "קרית אונו"])


def pool(n=12, base_ppm=20000, day="2026-09-01", street="הרצל"):
    return [bb.clean_deal(raw(i, day=day, amount=base_ppm * 80 + i * 1000, street=street), TODAY) for i in range(n)]


class FlagTests(unittest.TestCase):
    def test_bargain_flagged(self):
        deals = pool() + [bb.clean_deal(raw(99, day="2026-11-01", amount=int(20000 * 80 * 0.75)), TODAY)]
        flags, outliers = bb.find_flags(deals, cfg(), TODAY)
        self.assertEqual([f["id"] for f in flags], ["1099"])
        self.assertEqual(flags[0]["pct"], -25)
        self.assertEqual(outliers, 0)

    def test_too_cheap_is_outlier_not_bargain(self):
        deals = pool() + [bb.clean_deal(raw(99, day="2026-11-01", amount=int(20000 * 80 * 0.5)), TODAY)]
        flags, outliers = bb.find_flags(deals, cfg(), TODAY)
        self.assertEqual((flags, outliers), ([], 1))

    def test_old_deal_or_over_budget_ignored(self):
        old = bb.clean_deal(raw(98, day="2026-08-01", amount=int(20000 * 80 * 0.75)), TODAY)
        self.assertEqual(bb.find_flags(pool() + [old], cfg(), TODAY)[0], [])
        self.assertEqual(bb.find_flags(pool() + [bb.clean_deal(raw(99, day="2026-11-01", amount=1_200_000), TODAY)],
                                       cfg(budget=1_000_000), TODAY)[0], [])

    def test_needs_min_comps(self):
        deals = pool(n=3) + [bb.clean_deal(raw(99, day="2026-11-01", amount=int(20000 * 80 * 0.75)), TODAY)]
        self.assertEqual(bb.find_flags(deals, cfg(), TODAY)[0], [])

    def test_partial_share_dropped_everywhere(self):
        # GovMap לא מסמן חלק בנכס: 99 מ"ר ב-₪341K בעיר של ₪30K למ"ר הוא כמעט בוודאות חלק מדירה
        deals = pool(base_ppm=30000) + [bb.clean_deal(raw(99, amount=341_000, area=99, rooms=4), TODAY)]
        s = bb.city_summary("עיר", sorted(deals, key=lambda x: x["date"]), cfg(), TODAY)
        self.assertEqual(s["implausible"], 1)
        self.assertEqual(s["n"], 12)
        self.assertEqual(s["n_budget"], 0)


class StreetAndTableTests(unittest.TestCase):
    def test_cheap_street_listed(self):
        deals = pool(base_ppm=25000, street="יקר") + pool(base_ppm=17000, street="זול")
        s = bb.city_summary("עיר", sorted(deals, key=lambda x: x["date"]), cfg(), TODAY)
        self.assertEqual([r["street"] for r in s["streets"]], ["זול"])
        self.assertLess(s["streets"][0]["vs_city"], 0)

    def test_price_table_percentiles(self):
        t = bb.price_table(pool())
        n, p25, p50, p75 = t["3"][str(bb.area_band(80))]
        self.assertEqual(n, 12)
        self.assertTrue(p25 <= p50 <= p75)


class CacheTests(unittest.TestCase):
    def test_full_then_incremental(self):
        calls = []

        def fetch(name, d_from, d_to):
            calls.append(d_from)
            return [raw(1, day="2026-10-01"), raw(2, day="2026-11-10")]

        with tempfile.TemporaryDirectory() as tmp:
            d1, mode1 = bb.city_deals("עיר", ["עיר"], TODAY, 12, tmp, fetch)
            d2, mode2 = bb.city_deals("עיר", ["עיר"], TODAY, 12, tmp, fetch)
        self.assertEqual((mode1, mode2), ("full", "incremental"))
        self.assertEqual(calls[1], "2026-10-07")  # 45 ימים אחורה
        self.assertEqual(len(d1), 2)
        self.assertEqual(len(d2), 2)

    def test_names_change_forces_full(self):
        with tempfile.TemporaryDirectory() as tmp:
            bb.city_deals("עיר", ["עיר"], TODAY, 12, tmp, lambda *a: [])
            _, mode = bb.city_deals("עיר", ["עיר", "עיר2"], TODAY, 12, tmp, lambda *a: [])
        self.assertEqual(mode, "full")


def lot(tid, kind_type="מכרז פומבי רגיל", purpose="בנייה נמוכה/צמודת קרקע", units=1, deadline="2026-12-16 12:00:00", **kw):
    d = {"tender_id": tid, "tender_number": f"{tid[-3:]}/2026", "settlement": "עכו", "status": "מפורסם",
         "tender_type": kind_type, "purpose": purpose, "units": units, "deadline": deadline, "appraisal_price": 400000,
         "dev_costs": 150000, "threshold_price": None, "region": "חיפה", "neighborhood": "עכו",
         "tender_url": f"https://apps.land.gov.il/MichrazimSite/#/michraz/{tid}", "winner": "שם פרטי",
         "published": "2026-11-16"}
    d.update(kw)
    return d


class TenderTests(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(bb.tender_kind(lot("1")), "self")
        self.assertEqual(bb.tender_kind(lot("1", kind_type="הרשמה והגרלה", units=40)), "lottery")
        self.assertEqual(bb.tender_kind(lot("1", kind_type="מחיר מטרה", purpose="בנייה רוויה", units=200)), "target")
        self.assertEqual(bb.tender_kind(lot("1", purpose="בנייה רוויה", units=200)), "dev")

    def test_group_open_residential_only(self):
        lots = [lot("20260805"), lot("20260805", appraisal_price=300000),
                lot("20260806", deadline="2026-11-01 12:00:00"),          # עבר
                lot("20260807", purpose="תעשיה")]                          # לא למגורים
        out = bb.group_tenders(lots, TODAY)
        self.assertEqual([t["id"] for t in out], ["20260805"])
        t = out[0]
        self.assertEqual((t["lots"], t["units"], t["appraisal_per_unit"], t["dev_per_unit"]), (2, 2, 300000, 150000))
        self.assertNotIn("winner", t)  # לא שומרים שמות זוכים

    def test_unsafe_url_dropped(self):
        t = bb.group_tenders([lot("1", tender_url="javascript:alert(1)")], TODAY)[0]
        self.assertIsNone(t["url"])

    def test_history(self):
        hist = [{"purpose": "בנייה נמוכה/צמודת קרקע", "amount_per_unit": a, "dev_costs_per_unit": 100000, "bids": b}
                for a, b in ((500000, 10), (600000, 20), (700000, 30))]
        h = bb.tender_history("עכו", lambda p: hist, TODAY)
        self.assertEqual(h, {"n": 3, "per_unit": 700000, "bids": 20})
        self.assertIsNone(bb.tender_history("עכו", lambda p: hist[:2], TODAY))


class EligibilityTests(unittest.TestCase):
    """v1.9.0: אוכלוסיות לפי מגרש. קודם נשמרה רק האוכלוסייה של המגרש הראשון במכרז."""
    TODAY = date(2026, 11, 22)

    def mixed(self):
        lots = [lot("9", "הרשמה והגרלה", units=1, eligible_populations="חיילי מילואים; חסרי דיור") for _ in range(4)]
        lots += [lot("9", "הרשמה והגרלה", units=1, eligible_populations="כלל הציבור") for _ in range(2)]
        lots += [lot("9", "הרשמה והגרלה", units=2, eligible_populations="אנשים עם מוגבלות; חסרי דיור; כלל הציבור")]
        return bb.group_tenders(lots, self.TODAY)[0]

    def test_groups_per_lot(self):
        t = self.mixed()
        self.assertEqual(t["lots"], 7)
        self.assertEqual(t["groups"], [
            {"pops": ["אנשים עם מוגבלות", "חסרי דיור", "כלל הציבור"], "lots": 1, "units": 2},
            {"pops": ["חיילי מילואים", "חסרי דיור"], "lots": 4, "units": 4},
            {"pops": ["כלל הציבור"], "lots": 2, "units": 2}])
        self.assertEqual(t["eligible"], "אנשים עם מוגבלות; חיילי מילואים; חסרי דיור; כלל הציבור")

    def test_open_to_public_and_groups(self):
        t = self.mixed()
        self.assertEqual((t["open_lots"], t["open_units"]), (3, 4))          # רק כלל הציבור
        self.assertEqual(bb.open_to(t["groups"], ["חיילי מילואים"]), (7, 8))  # חייל מילואים רואה הכל

    def test_restricted_only_tender_closed_to_public(self):
        t = bb.group_tenders([lot("8", "הרשמה והגרלה", eligible_populations="חיילי מילואים; חסרי דיור")], self.TODAY)[0]
        self.assertEqual(t["open_lots"], 0)

    def test_no_population_means_open(self):
        t = bb.group_tenders([lot("7")], self.TODAY)[0]
        self.assertEqual((t["groups"][0]["pops"], t["open_lots"], t["eligible"]), (["ללא הגבלה"], 1, None))

    def test_notify_skips_closed_tenders(self):
        closed = {"id": "1", "kind": "lottery", "groups": [{"pops": ["חיילי מילואים"], "lots": 1, "units": 1}], "open_lots": 0}
        opened = {"id": "2", "kind": "lottery", "groups": [{"pops": ["כלל הציבור"], "lots": 1, "units": 1}], "open_lots": 1}
        legacy = {"id": "3", "kind": "lottery"}  # נתונים ישנים בלי groups
        t, _ = nb.collect_new({"tenders": [closed, opened, legacy], "cities": []}, {})
        self.assertEqual([x["id"] for x in t], ["2", "3"])


class BuildTests(unittest.TestCase):
    def test_city_failure_does_not_stop_build(self):
        def fetch(name, *a):
            if name == "רעה":
                raise api.ApiError("HTTP 500")
            return [raw(i) for i in range(6)]

        with tempfile.TemporaryDirectory() as tmp:
            out = bb.build(TODAY, cfg(cities=["טובה", "רעה"]), tmp, fetch, lambda p: [], names=["טובה", "רעה"])
        self.assertEqual([c["name"] for c in out["cities"]], ["טובה"])
        self.assertTrue(any("רעה" in e for e in out["errors"]))
        json.dumps(out, ensure_ascii=False)


class NotifyTests(unittest.TestCase):
    def data(self):
        return {"tenders": [{"id": "1", "kind": "self", "city": "עכו<b>", "deadline": "2026-12-16", "units": 1,
                             "url": "https://apps.land.gov.il/x"},
                            {"id": "2", "kind": "dev", "city": "חיפה", "deadline": "2026-12-16"}],
                "cities": [{"name": "לוד", "flags": [{"id": "f1", "pct": -20, "amount": 900000, "area": 70, "rooms": 3,
                                                      "date": "2026-11-01", "street": "הרצל", "house": "1"}]}]}

    def test_only_new_and_escaped(self):
        t, f = nb.collect_new(self.data(), {})
        self.assertEqual([x["id"] for x in t], ["1"])  # מכרזי יזמים לא נשלחים
        msg = nb.build_message(t, f)
        self.assertIn("עכו&lt;b&gt;", msg)
        self.assertIn('href="https://apps.land.gov.il/x"', msg)
        self.assertEqual(nb.collect_new(self.data(), {"tenders": ["1"], "flags": ["f1"]}), ([], []))
        self.assertIsNone(nb.build_message([], []))

    def test_long_message_cut_on_line(self):
        tenders = [dict(self.data()["tenders"][0], id=str(i), city="א" * 300) for i in range(30)]
        msg = nb.build_message(tenders, [])
        self.assertLessEqual(len(msg), nb.MAX_LEN)
        self.assertEqual(msg.count("<b>"), msg.count("</b>"))

    def test_send_failure_keeps_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            dp, sp = os.path.join(tmp, "b.json"), os.path.join(tmp, "s.json")
            with open(dp, "w", encoding="utf-8") as fh:
                json.dump(self.data(), fh)
            with mock.patch.object(nb, "DATA", dp), mock.patch.object(nb, "STATE", sp), \
                    mock.patch.object(nb, "send_telegram", return_value=False):
                self.assertEqual(nb.main(), 1)
            self.assertFalse(os.path.exists(sp))
            with mock.patch.object(nb, "DATA", dp), mock.patch.object(nb, "STATE", sp), \
                    mock.patch.object(nb, "send_telegram", return_value=True) as send:
                self.assertEqual(nb.main(), 0)
                self.assertTrue(send.call_args.kwargs.get("html_mode"))
            with open(sp, encoding="utf-8") as fh:
                self.assertEqual(json.load(fh), {"tenders": ["1"], "flags": ["f1"]})


class ProjectTests(unittest.TestCase):
    def test_new_building_split_from_market(self):
        market = pool(base_ppm=25000)
        proj = [bb.clean_deal(raw(200 + i, amount=int(15000 * 80), street=""), TODAY) for i in range(10)]
        for x in proj:
            x["gush"], x["helka"] = "7263", "12"
        s = bb.city_summary("עיר", sorted(market + proj, key=lambda x: x["date"]), cfg(), TODAY)
        self.assertEqual((s["n"], s["project_deals"]), (12, 10))
        self.assertEqual(s["flags_total"], 0)            # מכירה בפרויקט אינה "מציאה"
        p = s["projects"][0]
        self.assertTrue(p["discount"])
        self.assertLessEqual(p["vs_city"], -20)

    def test_small_parcel_or_with_street_stays_market(self):
        xs = [bb.clean_deal(raw(300 + i, street="הרצל"), TODAY) for i in range(10)]
        for x in xs:
            x["gush"], x["helka"] = "1", "1"
        market, projects = bb.split_projects(xs)
        self.assertEqual((len(market), len(projects)), (10, 0))


class TrendTests(unittest.TestCase):
    def test_mix_shift_is_not_a_trend(self):
        # אותו מחיר למ"ר בכל קבוצה, אבל ברבעון האחרון רק דירות קטנות (יקרות יותר למ"ר)
        old = [bb.clean_deal(raw(i, day="2026-03-01", amount=20000 * 80), TODAY) for i in range(25)] + \
              [bb.clean_deal(raw(100 + i, day="2026-03-01", amount=30000 * 50, area=50, rooms=2), TODAY) for i in range(25)]
        new = [bb.clean_deal(raw(200 + i, day="2026-10-01", amount=30000 * 50, area=50, rooms=2), TODAY) for i in range(25)]
        self.assertEqual(bb.price_trend(old + new, TODAY), 0.0)


class CspTests(unittest.TestCase):
    def test_hashes_match_inline_scripts(self):
        html = open(os.path.join(ROOT, "index.html"), encoding="utf-8").read()
        self.assertEqual(csp_hashes.inline_hashes(html), csp_hashes.current_hashes(html))

    def test_no_inner_html_in_new_tab(self):
        html = open(os.path.join(ROOT, "index.html"), encoding="utf-8").read()
        self.assertNotIn("innerHTML", html)


if __name__ == "__main__":
    unittest.main()
