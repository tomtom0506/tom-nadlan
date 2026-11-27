"""בדיקות ל-v2.1.4 ו-v2.2.0: שינוי מחיר לכל עיר (% שנה, % שנתיים), גרף מחיר למ"ר, מגמה ומדד הלמ"ס.
הקוד הוא JS בתוך index.html, בין סימוני mkchg; הבדיקה מריצה אותו ב-node.
"""
import json
import os
import re
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def js():
    with open(os.path.join(ROOT, "index.html"), encoding="utf-8") as f:
        return re.search(r"/\* mkchg-start \*/(.*?)/\* mkchg-end \*/", f.read(), re.S).group(1)


@unittest.skipUnless(shutil.which("node"), "node לא מותקן")
class MarketChangeTests(unittest.TestCase):
    def run_js(self, expr):
        out = subprocess.run(["node", "-e", js() + f"\nprocess.stdout.write(JSON.stringify({expr}));"],
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_mix_shift_is_not_a_price_change(self):
        # אותם מחירים לכל קבוצת חדרים, אבל השנה נמכרו בעיקר דירות 5 חדרים: ממוצע פשוט היה מראה עלייה גדולה
        base = {"3": {"n": 80, "s": 80 * 1_000_000}, "5": {"n": 20, "s": 20 * 2_000_000}}
        cur = {"3": {"n": 20, "s": 20 * 1_000_000}, "5": {"n": 80, "s": 80 * 2_000_000}}
        self.assertAlmostEqual(self.run_js(f"mixChange({json.dumps(cur)},{json.dumps(base)},20)"), 0.0)

    def test_weighted_by_current_counts(self):
        base = {"3": {"n": 50, "s": 50 * 1_000_000}, "4": {"n": 50, "s": 50 * 1_000_000}}
        cur = {"3": {"n": 75, "s": 75 * 1_100_000}, "4": {"n": 25, "s": 25 * 1_000_000}}  # +10% על 75%, 0% על 25%
        self.assertAlmostEqual(self.run_js(f"mixChange({json.dumps(cur)},{json.dumps(base)},20)"), 0.075)

    def test_too_few_deals_or_not_comparable(self):
        few = {"3": {"n": 10, "s": 10_000_000}}
        many = {"3": {"n": 50, "s": 50_000_000}}
        other = {"5": {"n": 50, "s": 50_000_000}}
        self.assertEqual(self.run_js(f"[mixChange({json.dumps(few)},{json.dumps(many)},20),"
                                     f"mixChange({json.dumps(many)},{json.dumps(few)},20),"
                                     f"mixChange({json.dumps(many)},{json.dumps(other)},20)]"), [None, None, None])

    def test_single_deal_group_is_ignored(self):
        # גבעתיים: "חדרים לא ידועים" - 19 עסקאות השנה מול עסקה אחת ב-0.3M. בלי סף, העיר "עלתה" ב-83%
        base = {"0": {"n": 1, "s": 300_000}, "3": {"n": 200, "s": 200 * 3_000_000}}
        cur = {"0": {"n": 19, "s": 19 * 4_500_000}, "3": {"n": 100, "s": 100 * 3_000_000}}
        self.assertAlmostEqual(self.run_js(f"mixChange({json.dumps(cur)},{json.dumps(base)},20)"), 0.0)

    def ppm_mo(self, price_of, ok_from=12, city="חיפה"):
        """48 חודשים; לכל חודש 3 חדרים: 30 עסקאות של 80 מ"ר במחיר price_of(i) למ"ר, ו-4 חדרים: 30 עסקאות במחיר קבוע."""
        months = [f"{2022 + (i + 6) // 12}-{(i + 6) % 12 + 1:02d}" for i in range(48)]
        ppm = []
        for i in range(ok_from, 48):
            ppm.append([i, 0, 3, 30, 30 * 80 * price_of(i), 30 * 80])
            ppm.append([i, 0, 4, 30, 30 * 100 * 20_000, 30 * 100])
        return {"months": months, "cities": [city], "rows": [], "ppm": ppm,
                "ppmOk": [1 if i >= ok_from else 0 for i in range(48)]}

    def test_city_windows_and_rooms_filter(self):
        # v2.2.0: 3 החודשים האחרונים מול אותם חודשים לפני שנה ושנתיים, מחיר למ"ר
        price = lambda i: 30_000 if i >= 45 else 33_000 if 33 <= i < 36 else 25_000 if 21 <= i < 24 else 28_000
        mo = self.ppm_mo(price)
        r = self.run_js(f"[cityChanges({json.dumps(mo)},3,50),cityChanges({json.dumps(mo)},null,50)]")
        self.assertAlmostEqual(r[0]["חיפה"]["y1"], 30 / 33 - 1)
        self.assertAlmostEqual(r[0]["חיפה"]["y2"], 30 / 25 - 1)
        self.assertAlmostEqual(r[1]["חיפה"]["y1"], (30 / 33 - 1) / 2)  # חצי מהעסקאות בלי שינוי

    def test_pending_until_months_are_collected(self):
        mo = self.ppm_mo(lambda i: 30_000, ok_from=30)  # רק 18 חודשים בפורמט החדש
        r = self.run_js(f"cityChanges({json.dumps(mo)},null,50)")["חיפה"]
        self.assertEqual((r["y1"], r["pending"], r["pending2"]), (0, False, True))
        mo = self.ppm_mo(lambda i: 30_000, ok_from=40)
        self.assertTrue(self.run_js(f"cityChanges({json.dumps(mo)},null,50)")["חיפה"]["pending"])

    def test_series_moving_average_and_trend_from_peak(self):
        # עלייה עד חודש 35 (שיא), ואז ירידה
        price = lambda i: 20_000 + 400 * i if i <= 35 else 34_000 - 1500 * (i - 35)
        mo = self.ppm_mo(price)
        pts = self.run_js(f"ppmSeries({json.dumps(mo)},null,3,5)")
        self.assertEqual(len(pts), 36)
        self.assertIsNone(pts[1]["ma"])
        self.assertAlmostEqual(pts[2]["ma"], (price(14) + price(13) + price(12)) / 3)
        t = self.run_js(f"ppmTrend({json.dumps(mo)},'חיפה',3,50)")
        self.assertEqual(t["peak"]["k"], mo["months"][35])
        cur = sum(price(i) for i in (45, 46, 47)) / 3
        peak = sum(price(i) for i in (33, 34, 35)) / 3
        self.assertAlmostEqual(t["peak"]["chg"], cur / peak - 1)
        self.assertLess(t["peak"]["chg"], 0)
        self.assertAlmostEqual(t["now"], cur)

    def test_unknown_city_has_no_data(self):
        mo = self.ppm_mo(lambda i: 30_000)
        t = self.run_js(f"ppmTrend({json.dumps(mo)},'עפולה',null,50)")
        self.assertIsNone(t["now"])
        self.assertIsNone(t["y1"])

    def test_cbs_trend(self):
        # מדד עולה ל-610 בחודש 30 ויורד ל-590
        vals = [550 + 2 * i for i in range(31)] + [610 - 5 * (i + 1) for i in range(17)]
        months = [f"{2022 + (i + 8) // 12}-{(i + 8) % 12 + 1:02d}" for i in range(48)]
        c = self.run_js(f"cbsTrend({json.dumps({'months': months, 'values': vals})})")
        now = sum(vals[-3:]) / 3
        self.assertAlmostEqual(c["y1"], now / (sum(vals[-15:-12]) / 3) - 1)
        self.assertAlmostEqual(c["y2"], now / (sum(vals[-27:-24]) / 3) - 1)
        self.assertEqual(c["peak"]["k"], months[30])
        self.assertLess(c["peak"]["chg"], 0)
        self.assertIsNone(self.run_js("cbsTrend({months:['2026-01'],values:[1]})"))


if __name__ == "__main__":
    unittest.main()
