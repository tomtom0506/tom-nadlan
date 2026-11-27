"""בדיקות offline ל-v1.6.0: שליפה מ-/api/deals/search, סיכום חודשי, אחסון, המרה מה-SQL הישן ובנייה מלאה.
הרצה: python3 -m unittest discover -s tests
"""
import os
import sys
import tempfile
import unittest
from datetime import date
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import build_dashboard as bd  # noqa: E402
import deals_api as api  # noqa: E402

APT = "דירה בבית קומות"


def raw(day, amount=1_000_000, area=80, rooms=3, city="חיפה", helka="1", sub="001", nature=APT, frac=1.0):
    return {"date": day, "amount": amount, "area_sqm": area, "rooms": rooms, "settlement": city, "nature": nature,
            "portion_fraction": frac, "portion": "1.000", "gush": "100", "helka": helka, "sub_parcel": sub,
            "year_built": 2000}


CBS = {"id": 40010, "name": "מדד מחירי דירות", "months": ["2026-08"], "values": [588.2]}


class CbsTests(unittest.TestCase):
    def test_parse_and_validate(self):
        pts = [{"year": 2022 + (i + 8) // 12, "month": (i + 8) % 12 + 1, "currBase": {"value": 500 + i}} for i in range(30)]
        pts.append({"year": 2026, "month": 9, "currBase": {"value": "bad"}})
        out = bd.fetch_cbs(get=lambda p: {"month": [{"code": 40010, "date": pts}]})
        self.assertEqual((out["months"][0], out["months"][-1], len(out["values"])), ("2022-09", "2025-02", 30))

    def test_too_few_points_raises(self):
        with self.assertRaises(api.ApiError):
            bd.fetch_cbs(get=lambda p: {"month": []})

    def test_failure_keeps_previous(self):
        prev = {"generated_at": "g", "cbs": CBS}
        self.assertEqual(bd.keep_previous("cbs", prev)["values"], [588.2])


class CleanDealTests(unittest.TestCase):
    def test_same_rules_as_sql(self):
        self.assertIsNotNone(api.clean_deal(raw("2026-05-01")))
        self.assertIsNone(api.clean_deal(raw("2026-05-01", frac=0.5)))           # לא הנכס כולו
        self.assertIsNone(api.clean_deal(raw("2026-05-01", amount=100_000)))     # מחיר לא סביר
        self.assertIsNone(api.clean_deal(raw("2026-05-01", amount=50_000_000)))
        self.assertIsNone(api.clean_deal(raw("2026-05-01", nature="מגורים")))    # לא דירה
        self.assertIsNone(api.clean_deal(raw("2026-05-01", city="")))

    def test_missing_rooms_and_area(self):
        x = api.clean_deal(raw("2026-05-01", rooms=None, area=None))
        self.assertEqual((x["rooms"], x["rb"], x["area"]), (0, 0, 0))

    def test_portion_text_fallback(self):
        d = raw("2026-05-01")
        d["portion_fraction"] = None
        self.assertIsNotNone(api.clean_deal(d))


class FetchTests(unittest.TestCase):
    def setUp(self):
        api._deadline = None

    def pages(self, rows, total=None, page=api.PAGE):
        """שרת מדומה: מחזיר דף לפי offset."""
        calls = []

        def fake(path, params):
            calls.append(params)
            off = params["offset"]
            return {"data": rows[off:off + page], "total": len(rows) if total is None else total, "total_capped": False}
        return fake, calls

    def test_pages_until_short_page(self):
        rows = [raw("2026-05-01", helka=str(i)) for i in range(450)]
        fake, calls = self.pages(rows)
        with mock.patch.object(api, "get_json", side_effect=fake):
            out, total, read = api.fetch_range(APT, "2026-05-01", "2026-05-31")
        self.assertEqual((len(out), total, read, len(calls)), (450, 450, 450, 3))
        self.assertEqual(calls[0]["limit"], 200)

    def test_duplicates_between_pages_dropped(self):
        rows = [raw("2026-05-01", helka=str(i)) for i in range(200)] + [raw("2026-05-01", helka="5")]
        fake, _ = self.pages(rows, total=201)
        with mock.patch.object(api, "get_json", side_effect=fake):
            out, total, _ = api.fetch_range(APT, "2026-05-01", "2026-05-31")
        self.assertEqual((len(out), total), (200, 201))

    def test_month_falls_back_to_days_when_paging_skips(self):
        # דפדוף לפי מיון לא ייחודי דילג על שורה: הספירה לא תואמת ל-total, ולכן שולפים יום-יום
        good = [raw(f"2026-05-{d:02d}", helka=str(d)) for d in range(1, 11)]

        def fake(path, params):
            if params["date_from"] != params["date_to"]:
                return {"data": good[:-1], "total": len(good)}
            return {"data": [r for r in good if r["date"] == params["date_from"]], "total": 1 if params["date_from"] <= "2026-05-10" else 0}
        with mock.patch.object(api, "get_json", side_effect=fake):
            out = api.fetch_nature_month(APT, "2026-05")
        self.assertEqual(len(out), 10)

    def test_month_incomplete_raises(self):
        def fake(path, params):
            return {"data": [], "total": 50}
        with mock.patch.object(api, "get_json", side_effect=fake):
            with self.assertRaises(api.ApiError):
                api.fetch_nature_month(APT, "2026-02")

    def test_capped_total_raises(self):
        with mock.patch.object(api, "get_json", return_value={"data": [], "total": 10000, "total_capped": True}):
            with self.assertRaises(api.ApiError):
                api.fetch_range(APT, "2026-05-01", "2026-05-31")


class SummarizeTests(unittest.TestCase):
    def test_ppm_second_hand_only(self):
        # v1.7.0: מחיר למ"ר רק לדירות יד שנייה (נבנו לפחות שנתיים לפני המכירה, או שנה לא ידועה)
        rows = [raw("2026-05-01", 2_000_000, 100, 3, helka="1"),                     # 2000: יד שנייה
                raw("2026-05-02", 3_000_000, 100, 3, helka="2"),                     # חדשה מקבלן
                raw("2026-05-03", 1_000_000, 50, 3, helka="3"),                      # שנה לא ידועה
                raw("2026-05-04", 300_000, 100, 3, helka="4"),                       # 3,000 ₪ למ"ר: לא עסקת שוק
                raw("2026-05-05", 1_000_000, 0, 3, helka="5")]                       # בלי שטח
        rows[1]["year_built"] = 2025
        rows[2]["year_built"] = None
        deals = [api.clean_deal(d) for d in rows]
        rec = api.summarize("2026-05", deals)
        self.assertEqual(rec["ppm"], [["חיפה", 3, 2, 3_000_000, 150.0]])
        self.assertEqual(rec["fmt"], api.FORMAT)
        self.assertEqual(rec["n"], 5)  # הספירה הרגילה לא השתנתה


    def test_days_monthly_budget(self):
        deals = [api.clean_deal(d) for d in (
            raw("2026-05-01", 1_000_000, 80, 3), raw("2026-05-01", 2_000_000, 100, 3, helka="2"),
            raw("2026-05-02", 1_400_000, 20, 4.5, helka="3"), raw("2026-04-30", helka="4"))]
        rec = api.summarize("2026-05", deals)
        self.assertEqual(rec["n"], 3)
        self.assertEqual(rec["days"][0], ["2026-05-01", "חיפה", 3, 2, 3_000_000, 1_000_000, 2_000_000])
        self.assertEqual(rec["monthly"], [["חיפה", 3, 2, 3_000_000, 1_000_000, 2_000_000],
                                          ["חיפה", 4, 1, 1_400_000, 1_400_000, 1_400_000]])
        n_all, s_area, area, inb = rec["budget"]["חיפה"]
        self.assertEqual((n_all, s_area, area), (3, 3_000_000, 180))  # 20 מ"ר מחוץ לטווח הסביר
        self.assertEqual(inb, [[1_000_000, 80], [1_400_000, 20]])


class StoreTests(unittest.TestCase):
    def test_save_load_list(self):
        with tempfile.TemporaryDirectory() as root:
            api.save_month({"month": "2026-05", "n": 1}, root)
            self.assertEqual(api.list_months(root), ["2026-05"])
            self.assertEqual(api.load_month("2026-05", root)["n"], 1)
            self.assertIsNone(api.load_month("2026-04", root))

    def test_sharp_drop_vs_previous_api_does_not_overwrite(self):
        # בדיקת האמינות: שליפה שמחזירה הרבה פחות עסקאות משליפה קודמת מה-API לא דורסת אותה
        with tempfile.TemporaryDirectory() as root:
            api.save_month({"month": "2026-05", "n": 100, "source": "api", "monthly": []}, root)
            few = [raw("2026-05-01", helka=str(i)) for i in range(90)]
            with self.assertRaises(api.ApiError):
                api.refresh_month("2026-05", root, fetch=lambda m: few)
            self.assertEqual(api.load_month("2026-05", root)["n"], 100)
            many = [raw("2026-05-01", helka=str(i)) for i in range(98)]
            rec, deals = api.refresh_month("2026-05", root, fetch=lambda m: many)
            self.assertEqual((rec["n"], rec["source"], len(deals)), (98, "api", 98))

    def test_legacy_sql_is_replaced_even_if_higher(self):
        # deals_probe 1.2.0: ב-SQL הישן חלק מהעסקאות נספרו פעמיים (מגדל העמק 12.07: 78 מול 39). ה-API מחליף אותו
        with tempfile.TemporaryDirectory() as root:
            api.save_month({"month": "2026-07", "n": 220, "source": "sql-legacy", "monthly": []}, root)
            rows = [raw("2026-07-12", helka=str(i)) for i in range(164)]
            rec, _ = api.refresh_month("2026-07", root, fetch=lambda m: rows)
            self.assertEqual((rec["n"], api.load_month("2026-07", root)["source"]), (164, "api"))

    def test_legacy_roundtrip(self):
        dash = {"generated_at": "g", "days": ["2026-05-01", "2026-07-02"], "cities": ["א", "ב"],
                "rows": [[0, 0, 3.0, 2, 2_000_000, 900_000, 1_100_000], [1, 1, 4.5, 1, 1_500_000, 1_500_000, 1_500_000]],
                "monthly": {"months": ["2026-04", "2026-05"], "cities": ["א"], "rows": [[1, 0, 3, 2, 2_000_000, 900_000, 1_100_000]]}}
        out = api.legacy_months(dash)
        self.assertEqual(sorted(out), ["2026-05", "2026-07"])
        self.assertEqual(out["2026-05"]["n"], 2)
        self.assertEqual(out["2026-07"]["monthly"], [["ב", 4, 1, 1_500_000, 1_500_000, 1_500_000]])  # מתוך הימים
        self.assertEqual(out["2026-05"]["source"], "sql-legacy")


class PlanTests(unittest.TestCase):
    def test_recent_and_watch_first_then_missing(self):
        have = {m: "api" for m in bd.months_window("2026-09", bd.KEEP_MONTHS)}
        have["2025-11"] = "sql-legacy"
        fmt = {m: api.FORMAT for m in have}
        plan = bd.plan_months("2026-09", have, extra=("2026-04", "2026-05", "2026-06"), fmt=fmt)
        self.assertEqual(plan, ["2026-09", "2026-08", "2026-07", "2026-06", "2026-05", "2026-04", "2025-11"])

    def test_old_format_in_trend_window_is_refetched_newest_first(self):
        # v1.7.0: קבצים בפורמט 1 (בלי ppm) בחלון המגמה נשלפים מחדש, מהחדש לישן, לפני חודשים ישנים יותר שחסרים
        window = bd.months_window("2026-09", bd.KEEP_MONTHS)
        have = {m: "api" for m in window}
        have["2022-08"] = "sql-legacy"
        fmt = {m: api.FORMAT for m in window}
        fmt["2025-01"] = 1
        fmt["2024-02"] = None
        fmt["2022-09"] = 1  # מחוץ לחלון המגמה: לא נשלף שוב
        plan = bd.plan_months("2026-09", have, fmt=fmt)
        self.assertEqual(plan[4:], ["2025-01", "2024-02", "2022-08"])

    def test_out_of_time_stops_and_reports(self):
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.object(api, "time_left", return_value=10):
                deals, errors, left = bd.refresh("2026-09", root, fetch=mock.Mock())
        self.assertEqual((deals, errors), ({}, []))
        self.assertEqual(len(left), bd.KEEP_MONTHS)


class BuildTests(unittest.TestCase):
    """בנייה מלאה מול API מדומה: כל חלון ההיסטוריה, עם עוגן, תקציב ומעקב."""

    def fake_month(self, month):
        n = 30 if month < "2026-09" else 3  # החודש האחרון חלקי, כמו במאגר האמיתי
        out = []
        for i in range(n):
            out.append(raw(f"{month}-{(i % 28) + 1:02d}", 1_000_000 + i * 1000, 80 + i % 5, 3, city="רמת גן",
                           helka=str(i)))
        return out

    def test_end_to_end(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = {"cities": ["רמת גן"], "pct": 15, "cap": 45, "max_flags": 3}
            with mock.patch.object(api, "stats", return_value={"last_deal": "2026-09-05",
                                                               "scraped_at": "2026-09-19 11:07:39.0+00"}), \
                    mock.patch.object(bd, "load_watch", return_value=cfg), \
                    mock.patch.object(bd, "fetch_cbs", return_value=CBS):
                out = bd.build(today=date(2026, 11, 20), prev={}, root=root, fetch=self.fake_month)
            self.assertEqual(out["anchor_month"], "2026-08")
            self.assertEqual(out["anchor"], "2026-08-28")
            self.assertEqual(out["fetch_errors"], [])
            self.assertEqual(out["months_missing"], [])
            self.assertEqual(out["budget"]["rows"][0][:3], ["רמת גן", 360, 360])
            self.assertFalse(out["budget"].get("stale"))
            self.assertEqual(out["watch"]["cities"][0]["name"], "רמת גן")
            self.assertEqual(out["watch"]["ref_months"], ["2026-06", "2026-07", "2026-08"])
            self.assertEqual(out["db_scraped_at"], "2026-09-19 11:07:39")
            self.assertEqual(out["monthly"]["months"][-1], "2026-08")
            self.assertEqual(len(api.list_months(root)), bd.KEEP_MONTHS)
            self.assertEqual(len(out["monthly"]["months"]), 48)
            self.assertEqual(sum(r[3] for r in out["monthly"]["rows"]), 48 * 30)
            self.assertEqual(sum(r[3] for r in out["rows"]), 30 * 3 + 3)  # 3 חודשי העוגן ועוד ספטמבר
            self.assertEqual(out["cbs"]["values"][-1], 588.2)
            ppm_ok = out["monthly"]["ppm_ok"]
            self.assertEqual((len(ppm_ok), sum(ppm_ok)), (48, bd.PPM_BACK))  # רק 36 החודשים האחרונים
            self.assertEqual({r[0] for r in out["monthly"]["ppm"]}, set(range(12, 48)))
            self.assertEqual(sum(r[3] for r in out["monthly"]["ppm"]), 36 * 30)


if __name__ == "__main__":
    unittest.main()
