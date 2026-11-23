"""בדיקות לחישוב במחשבון "האם המחיר הזה מציאה" (v2.0.0): פענוח קומה, פענוח כתובת, בחירת עסקאות דומות והתאמות.
הקוד הוא JS בתוך index.html; הבדיקה מחלצת את הפונקציות הטהורות (בין סימוני calc-pure) ומריצה אותן ב-node.
הרצה: python3 -m unittest discover -s tests (מדלגת אם node לא מותקן)
"""
import json
import os
import re
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def pure_js():
    with open(os.path.join(ROOT, "index.html"), encoding="utf-8") as f:
        s = f.read()
    parts = [re.search(rf"/\* {m}-start \*/(.*?)/\* {m}-end \*/", s, re.S).group(1)
             for m in ("calc-pure", "calc-pure3", "calc-pure2")]
    return "function fin(v){return typeof v==='number'&&isFinite(v);}\n" + "\n".join(parts)


@unittest.skipUnless(shutil.which("node"), "node לא מותקן")
class CalcJsTests(unittest.TestCase):
    def run_js(self, expr):
        code = pure_js() + f"\nprocess.stdout.write(JSON.stringify({expr}));"
        out = subprocess.run(["node", "-e", code], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_floor_words(self):
        self.assertEqual(self.run_js("['קרקע','ראשונה','שניה','שנייה','רביעית','עשירית','שתים עשרה','3',null,'גג'].map(floorNum)"),
                         [0, 1, 2, 2, 4, 10, 12, 3, None, None])

    def test_comps_filter(self):
        rows = [
            {"deal_nature": "דירה בבית קומות", "asset_area": 100, "deal_amount": 3000000, "rooms": 4, "floor": "שלישית"},
            {"deal_nature": "דירה בבית קומות", "asset_area": 140, "deal_amount": 3000000, "rooms": 4},  # שטח רחוק
            {"deal_nature": "דירה בבית קומות", "asset_area": 100, "deal_amount": 3000000, "rooms": 7},  # חדרים רחוקים
            {"deal_nature": "קרקע למגורים", "asset_area": 100, "deal_amount": 3000000, "rooms": 4},     # לא דירה
            {"deal_nature": "דירה בבית קומות", "asset_area": 100, "deal_amount": 100000, "rooms": 4}]   # מחיר לא סביר
        r = self.run_js(f"compsOf({json.dumps(rows, ensure_ascii=False)},100,4)")
        self.assertEqual(r, [{"ppm": 30000, "floor": 3}])

    def adj(self, **o):
        base = {"scope": "street", "floor": None, "compFloor": None, "lift": "", "mamad": "", "balcony": "",
                "park": "", "store": "", "cond": "", "year": None}
        base.update(o)
        return self.run_js(f"adjustments({json.dumps(base, ensure_ascii=False)})")

    def test_no_adjustments_when_unknown(self):
        self.assertEqual(self.adj(), {"items": [], "total": 0})

    def test_old_without_lift_and_mamad(self):
        r = self.adj(floor=4, compFloor=2, lift="no", mamad="no", balcony="no", cond="old")
        self.assertEqual([x["v"] for x in r["items"]], [-5, -4, -3, -8])
        self.assertEqual(r["total"], -20)

    def test_building_scope_skips_building_traits(self):
        # בהשוואה לאותו בניין: ממ"ד וגיל זהים לעסקאות, ולכן לא מותאמים
        r = self.adj(scope="building", mamad="no", year=1960, floor=3, compFloor=3)
        self.assertEqual(r["items"], [])

    def test_age_only_at_city_scope(self):
        self.assertEqual(self.adj(scope="city", year=1970)["total"], -6)
        self.assertEqual(self.adj(scope="street", year=1970)["total"], 0)

    def test_floor_with_lift_and_ground(self):
        self.assertEqual(self.adj(floor=6, compFloor=2, lift="yes")["total"], 4)
        self.assertEqual(self.adj(floor=0, compFloor=2)["total"], -5)  # ‎-2 קומות, ועוד ‎-3 קומת קרקע

    def test_total_capped(self):
        r = self.adj(scope="city", floor=9, compFloor=2, lift="no", mamad="no", balcony="no", park="no", cond="old", year=1950)
        self.assertEqual(r["total"], -25)

    def test_city_key_merges_spellings(self):
        # v2.1.0: "קרית אונו" (4 עסקאות ב-GovMap) ו"קריית אונו" (2,208) הם אותה עיר
        r = self.run_js("[cityKey('קרית אונו')===cityKey('קריית אונו'), cityKey('תל אביב-יפו')===cityKey('תל אביב -יפו'),"
                        " cityKey('יהוד')===cityKey('יהוד-מונוסון')]")
        self.assertEqual(r, [True, True, False])

    def test_pick_street(self):
        names = ["צה\"ל", "צהלה", "שדרות שמעון פרס"]
        r = self.run_js(f"[pickStreet({json.dumps(names, ensure_ascii=False)},'צהל'),"
                        f"pickStreet({json.dumps(names, ensure_ascii=False)},'שמעון פרס'),"
                        f"pickStreet({json.dumps(names, ensure_ascii=False)},'הרצל'),pickStreet([],'צהל')]")
        self.assertEqual(r, ['צה"ל', "שדרות שמעון פרס", None, None])  # "צהל" מתאים בדיוק ל"צה"ל" ולא ל"צהלה"

    def test_wide_comps_ignore_rooms(self):
        rows = [{"deal_nature": "דירה בבית קומות", "asset_area": 130, "deal_amount": 2600000, "rooms": 6},
                {"deal_nature": "דירה בבית קומות", "asset_area": 150, "deal_amount": 3000000, "rooms": 4}]
        self.assertEqual([x["ppm"] for x in self.run_js(f"wideComps({json.dumps(rows, ensure_ascii=False)},100)")], [20000])

    def test_quantiles(self):
        self.assertEqual(self.run_js("[quant([1,2,3,4,5],0.5),quant([1,2,3,4],0.25),quant([],0.5)]"), [3, 1.75, None])

    def test_suggest_prefix_first_limited_no_dup_spellings(self):
        # v2.1.2: במקום datalist עם 1,167 ערים, שנתקע בטלפון - עד 8 הצעות, קודם התחלה ואחר כך הכלה
        names = ["אונו", "קרית אונו", "קריית אונו", "קריית אתא", "גני תקווה", "אור יהודה"]
        self.assertEqual(self.run_js(f"suggest({json.dumps(names, ensure_ascii=False)},'קרית א')"),
                         ["קרית אונו", "קריית אתא"])
        self.assertEqual(self.run_js(f"suggest({json.dumps(names, ensure_ascii=False)},'אונו')"),
                         ["אונו", "קרית אונו"])
        many = [f"עיר {i}" for i in range(1200)]
        self.assertEqual(len(self.run_js(f"suggest({json.dumps(many, ensure_ascii=False)},'עיר')")), 8)
        self.assertEqual(self.run_js("suggest(['א'],'  ')"), [])

    def test_est_sqm(self):
        # v2.1.3: בלי שטח משערים לפי עסקאות עם אותו מספר חדרים
        rows = [{"deal_nature": "דירה בבית קומות", "asset_area": a, "rooms": r}
                for a, r in ((70, 3), (75, 3), (80, 3), (110, 4), (120, 5))]
        js = json.dumps(rows, ensure_ascii=False)
        self.assertEqual(self.run_js(f"[estSqm({js},3),estSqm({js},0),estSqm([],4),estSqm([],0)]"),
                         [{"v": 75, "src": "rows"}, {"v": 80, "src": "rows"}, {"v": 105, "src": "rooms"}, None])

    def test_tax_comps_houses_no_land_no_partial(self):
        rows = [{"nature": "קוטג' חד משפחתי", "area_sqm": 100, "amount": 2000000, "portion_fraction": 1.0},
                {"nature": "מגורים", "area_sqm": 100, "amount": 2000000, "portion_fraction": 1.0},       # קרקע
                {"nature": "דירה בבית קומות", "area_sqm": 100, "amount": 2000000, "portion_fraction": 0.5},  # חלק
                {"nature": "בית בודד", "area_sqm": 300, "amount": 2000000, "portion_fraction": None},      # שטח רחוק
                {"nature": "בית בודד", "area_sqm": 130, "amount": 200000, "portion_fraction": 1.0}]        # העברה בקיבוץ: 1,538 ₪ למ"ר
        self.assertEqual(self.run_js(f"taxComps({json.dumps(rows, ensure_ascii=False)},100)"), [{"ppm": 20000, "floor": None}])

    def test_near_towns_subdistrict_then_district(self):
        lst = [{"name": "א", "subdistrict": "גולן", "district": "צפון", "population": 5},
               {"name": "ב", "subdistrict": "גולן", "district": "צפון", "population": 9},
               {"name": "ג", "subdistrict": "כנרת", "district": "צפון", "population": 50},
               {"name": "עין זיוון", "subdistrict": "גולן", "district": "צפון", "population": 500}]
        js = json.dumps(lst, ensure_ascii=False)
        self.assertEqual(self.run_js(f"nearTowns({js},{{name:'עין זיוון',subdistrict:'גולן',district:'צפון'}},3)"), ["ב", "א"])
        self.assertEqual(self.run_js(f"nearTowns({js},{{name:'ד',subdistrict:'עמק',district:'צפון'}},2)"), ["עין זיוון", "ג"])

    def test_accuracy_bounds_and_order(self):
        tight = {"n": 20, "p25": 29000, "p50": 30000, "p75": 31000}
        r = self.run_js(f"[accuracy({json.dumps(tight)},'building',false,0,false),"
                        f"accuracy({json.dumps(tight)},'city',false,0,false),"
                        f"accuracy({json.dumps(tight)},'region',true,-20,'rows'),"
                        f"accuracy({{n:1,p25:9000,p50:30000,p75:60000}},'region',true,0,true)]")
        self.assertEqual(r[0], 5)                # מינימום
        self.assertLess(r[0], r[1])
        self.assertLess(r[1], r[2])
        self.assertEqual(r[3], 40)               # מקסימום
