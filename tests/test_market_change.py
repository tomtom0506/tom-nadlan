"""בדיקות ל-v2.1.4: אחוז שינוי במחיר הממוצע לכל עיר (% שנה, % שנתיים), מתוקן תמהיל חדרים.
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

    def test_city_windows_and_rooms_filter(self):
        months = [f"20{2 + (i + 6) // 12 + 21}-{(i + 6) % 12 + 1:02d}" for i in range(48)]
        rows = []
        for i in range(12, 48):  # 36 החודשים האחרונים: לפני שנתיים 1.0M, שנה שעברה 1.1M, השנה 1.21M
            price = 1_000_000 if i < 24 else 1_100_000 if i < 36 else 1_210_000
            rows.append([i, 0, 3, 3, 3 * price, price, price])
            rows.append([i, 0, 4, 3, 3 * 5_000_000, 5_000_000, 5_000_000])  # 4 חדרים: ללא שינוי
        mo = {"months": months, "cities": ["חיפה"], "rows": rows}
        r = self.run_js(f"[cityChanges({json.dumps(mo)},3,20),cityChanges({json.dumps(mo)},null,20)]")
        self.assertAlmostEqual(r[0]["חיפה"]["y1"], 0.1)
        self.assertAlmostEqual(r[0]["חיפה"]["y2"], 0.21)
        self.assertAlmostEqual(r[1]["חיפה"]["y1"], 0.05)  # חצי מהעסקאות +10%, חצי 0%


if __name__ == "__main__":
    unittest.main()
