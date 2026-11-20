"""בדיקות offline ל-v1.5.5: מעקב דטרמיניסטי, הזדמנויות לפי עסקאות דומות, סינון סבירות בתקציב, דיוק מלא.
הרצה: python3 -m unittest discover -s tests
"""
import os
import random
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import build_dashboard as bd  # noqa: E402

CFG = {"cities": ["רמת גן"], "pct": 15, "cap": 45, "max_flags": 3}


def deal(i, rooms, area, ppm, month="06"):
    return {"row_hash": f"h{i:04d}", "deal_date": f"15/{month}/2026", "settlement": "רמת גן",
            "deal_amount": str(round(area * ppm)), "asset_area": str(area), "room_num": str(rooms),
            "gush": "1", "chelka": "2", "sub_chelka": "0", "year_built": "2000"}


def market():
    """שוק של 4 חדרים: דירות של 90 מ"ר ב-30K למ"ר ודירות של 140 מ"ר ב-22K למ"ר."""
    rows = [deal(i, 4, 90, 30000 + i * 10, "05") for i in range(8)]
    rows += [deal(100 + i, 4, 140, 22000 + i * 10, "05") for i in range(8)]
    return rows


def run_watch(rows, page=bd.PAGE_M):
    pages = [rows[i:i + page] for i in range(0, len(rows), page)] or [[]]
    if len(pages[-1]) == page:
        pages.append([])
    sqls = []

    def fake(sql):
        sqls.append(sql)
        return pages[len(sqls) - 1]
    with mock.patch.object(bd, "post_sql", side_effect=fake):
        return bd.fetch_watch("2026-06", CFG), sqls


class WatchTests(unittest.TestCase):
    def test_pagination_orders_by_unique_column(self):
        # באג 3: מיון לפי deal_date בלבד (לא ייחודי) נתן תוצאות שונות בכל יום
        _, sqls = run_watch(market())
        self.assertIn("ORDER BY row_hash", sqls[0])

    def test_duplicates_across_pages_are_dropped(self):
        rows = market()
        out1, _ = run_watch(rows)
        out2, _ = run_watch(rows + rows[:5])
        self.assertEqual(out1, out2)

    def test_same_data_any_order_same_result(self):
        rows = market() + [deal(900, 4, 92, 24000)]
        base, _ = run_watch(rows)
        for seed in range(5):
            r = rows[:]
            random.Random(seed).shuffle(r)
            self.assertEqual(run_watch(r)[0], base)

    def test_compares_to_similar_size_not_whole_room_bucket(self):
        # באג 8: 140 מ"ר ב-22K למ"ר זה מחיר שוק לגודל הזה, למרות שהוא נמוך מהחציון של כל 4 החדרים
        out, _ = run_watch(market() + [deal(900, 4, 140, 21800)])
        self.assertEqual(out["cities"][0]["flags_total"], 0)
        # 92 מ"ר ב-24K למ"ר הוא כ-20% מתחת לדירות הדומות (90 מ"ר, ~30K)
        out, _ = run_watch(market() + [deal(901, 4, 92, 24000)])
        c = out["cities"][0]
        self.assertEqual(c["flags_total"], 1)
        f = c["flags"][0]
        self.assertEqual(f["n_comps"], 8)
        self.assertEqual(f["pct"], -20)
        self.assertNotIn("plausible", f)

    def test_implausible_area_is_not_flagged(self):
        # המקרה מהדוח: 2 חדרים ו-115 מ"ר (57 מ"ר לחדר) סומן כהזדמנות של ‎-40%
        rows = [deal(i, 2, 110 + i, 30000, "05") for i in range(8)] + [deal(900, 2, 115, 18000)]
        out, _ = run_watch(rows)
        c = out["cities"][0]
        self.assertEqual(c["flags_total"], 0)
        self.assertEqual(c["implausible"], 1)

    def test_needs_min_comps(self):
        rows = market()[:bd.MIN_COMPS - 1] + [deal(900, 4, 90, 24000)]
        out, _ = run_watch(rows)
        self.assertEqual(out["cities"][0]["flags_total"], 0)

    def test_plausible(self):
        mk = lambda rooms, area: {"rooms": rooms, "rb": bd.rooms_bucket(rooms), "area": area}  # noqa: E731
        self.assertTrue(bd.plausible(mk(3, 80)))
        self.assertFalse(bd.plausible(mk(2, 115)))
        self.assertFalse(bd.plausible(mk(5, 40)))
        self.assertFalse(bd.plausible(mk(None, 80)))


class BudgetTests(unittest.TestCase):
    ROW = {"settlement": "הוד השרון", "n_all": 100, "n_b": 5, "s_b": 6e6, "na_b": 5, "a_b": 400, "sa_b": 6e6}

    def test_query_filters_low_ppm(self):
        # באג 9: עסקאות ב-₪4,232 למ"ר הובילו את הלשונית
        sql = bd.budget_sql("deal_date LIKE '%/06/2026'")
        self.assertIn("LEFT JOIN", sql)
        self.assertIn(f"{bd.PPM_FLOOR} * c.ref", sql)
        self.assertIn("GROUP BY t.settlement", sql)

    def test_fallback_when_server_rejects_join(self):
        calls = []

        def fake(sql):
            calls.append(sql)
            if "JOIN" in sql:
                raise bd.SqlError("HTTP 400")
            return [self.ROW]
        with mock.patch.object(bd, "post_sql", side_effect=fake):
            out = bd.fetch_budget("2026-06")
        self.assertFalse(out["ppm_filter"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(out["rows"][0][0], "הוד השרון")

    def test_network_failure_is_not_masked_by_fallback(self):
        with mock.patch.object(bd, "post_sql", side_effect=bd.SqlError("retries exhausted")):
            with self.assertRaises(bd.SqlError):
                bd.fetch_budget("2026-06")

    def test_filter_flag_on_success(self):
        with mock.patch.object(bd, "post_sql", return_value=[self.ROW]):
            out = bd.fetch_budget("2026-06")
        self.assertTrue(out["ppm_filter"])
        self.assertEqual(out["ppm_floor"], bd.PPM_FLOOR)


class PrecisionTests(unittest.TestCase):
    def test_no_float4_casts(self):
        # באג 13: REAL הוא float4, והסכומים "רעשו" בין הרצות
        with open(bd.__file__, encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn("AS REAL)", src)
        self.assertIn("DOUBLE PRECISION", bd.AMT)


if __name__ == "__main__":
    unittest.main()
