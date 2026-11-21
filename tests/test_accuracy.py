"""בדיקות offline: מעקב דטרמיניסטי והזדמנויות לפי עסקאות דומות (v1.5.5), סינון סבירות בתקציב.
מאז v1.6.0 החישוב רץ על עסקאות מה-API (deals_api.clean_deal) ועל סיכומים חודשיים, ולא על SQL.
הרצה: python3 -m unittest discover -s tests
"""
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import build_dashboard as bd  # noqa: E402
import deals_api as api  # noqa: E402

CFG = {"cities": ["רמת גן"], "pct": 15, "cap": 45, "max_flags": 3}


def deal(i, rooms, area, ppm, month="06"):
    return {"date": f"2026-{month}-15", "city": "רמת גן", "rooms": rooms, "rb": api.rooms_bucket(rooms),
            "amount": round(area * ppm), "area": area, "gush": "1", "helka": str(i), "sub": "0", "year": 2000}


def market():
    """שוק של 4 חדרים: דירות של 90 מ"ר ב-30K למ"ר ודירות של 140 מ"ר ב-22K למ"ר."""
    rows = [deal(i, 4, 90, 30000 + i * 10, "05") for i in range(8)]
    rows += [deal(100 + i, 4, 140, 22000 + i * 10, "05") for i in range(8)]
    return rows


def run_watch(rows):
    return bd.fetch_watch("2026-06", CFG, rows)


class WatchTests(unittest.TestCase):
    def test_duplicates_are_dropped(self):
        rows = market()
        self.assertEqual(run_watch(rows), run_watch(rows + rows[:5]))

    def test_same_data_any_order_same_result(self):
        # באג 3: אותם נתונים חייבים לתת אותה תוצאה, בלי תלות בסדר שבו הגיעו
        rows = market() + [deal(900, 4, 92, 24000), deal(901, 4, 91, 24100)]
        base = run_watch(rows)
        for seed in range(5):
            r = rows[:]
            random.Random(seed).shuffle(r)
            self.assertEqual(run_watch(r), base)

    def test_compares_to_similar_size_not_whole_room_bucket(self):
        # באג 8: 140 מ"ר ב-22K למ"ר זה מחיר שוק לגודל הזה, למרות שהוא נמוך מהחציון של כל 4 החדרים
        out = run_watch(market() + [deal(900, 4, 140, 21800)])
        self.assertEqual(out["cities"][0]["flags_total"], 0)
        out = run_watch(market() + [deal(901, 4, 92, 24000)])
        c = out["cities"][0]
        self.assertEqual(c["flags_total"], 1)
        f = c["flags"][0]
        self.assertEqual(f["n_comps"], 8)
        self.assertEqual(f["pct"], -20)
        self.assertEqual(f["chelka"], "901")  # שם השדה שהאפליקציה מצפה לו
        self.assertNotIn("plausible", f)

    def test_implausible_area_is_not_flagged(self):
        # המקרה מהדוח: 2 חדרים ו-115 מ"ר (57 מ"ר לחדר) סומן כהזדמנות של ‎-40%
        rows = [deal(i, 2, 110 + i, 30000, "05") for i in range(8)] + [deal(900, 2, 115, 18000)]
        c = run_watch(rows)["cities"][0]
        self.assertEqual(c["flags_total"], 0)
        self.assertEqual(c["implausible"], 1)

    def test_needs_min_comps(self):
        rows = market()[:bd.MIN_COMPS - 1] + [deal(900, 4, 90, 24000)]
        self.assertEqual(run_watch(rows)["cities"][0]["flags_total"], 0)

    def test_outside_window_and_area_range_ignored(self):
        rows = market() + [deal(900, 4, 92, 24000, "02"), deal(901, 4, 500, 5000)]
        out = run_watch(rows)
        self.assertEqual(out["cities"][0]["flags_total"], 0)
        self.assertEqual(out["ref_months"], ["2026-04", "2026-05", "2026-06"])

    def test_plausible(self):
        mk = lambda rooms, area: {"rooms": rooms, "rb": api.rooms_bucket(rooms), "area": area}  # noqa: E731
        self.assertTrue(bd.plausible(mk(3, 80)))
        self.assertFalse(bd.plausible(mk(2, 115)))
        self.assertFalse(bd.plausible(mk(5, 40)))
        self.assertFalse(bd.plausible(mk(0, 80)))


class BudgetTests(unittest.TestCase):
    MONTHS = bd.months_window("2026-06", back=12)

    def recs(self, city_budget):
        return {m: {"month": m, "budget": city_budget if m == "2026-06" else {}} for m in self.MONTHS}

    def test_low_ppm_deals_not_counted(self):
        # באג 9: עסקאות ב-₪4,232 למ"ר הובילו את הלשונית. ממוצע העיר כאן 30K למ"ר; עסקה ב-4K למ"ר נופלת
        deals = [[1_200_000, 100], [1_400_000, 60], [400_000, 100]]
        out = bd.build_budget("2026-06", self.recs({"הוד השרון": [10, 30_000 * 500, 500, deals]}))
        name, n_all, n_b, s_b, na_b, a_b, sa_b = out["rows"][0]
        self.assertEqual((name, n_all, n_b, s_b, na_b, a_b), ("הוד השרון", 10, 2, 2_600_000, 2, 160))
        self.assertTrue(out["ppm_filter"])
        self.assertEqual((out["from"], out["to"]), ("2025-07", "2026-06"))

    def test_deals_without_area_still_counted(self):
        out = bd.build_budget("2026-06", self.recs({"א": [3, 0, 0, [[900_000, 0], [1_000_000, 0]]]}))
        self.assertEqual(out["rows"][0][1:4], [3, 2, 1_900_000])
        self.assertEqual(out["rows"][0][4], 0)

    def test_missing_month_raises(self):
        recs = self.recs({"א": [1, 0, 0, []]})
        del recs["2025-09"]
        with self.assertRaises(bd.SqlError):
            bd.build_budget("2026-06", recs)

    def test_sums_across_months(self):
        recs = {m: {"month": m, "budget": {"א": [2, 20_000 * 100, 100, [[1_000_000, 50]]]}} for m in self.MONTHS}
        row = bd.build_budget("2026-06", recs)["rows"][0]
        self.assertEqual(row[1:3], [24, 12])


if __name__ == "__main__":
    unittest.main()
