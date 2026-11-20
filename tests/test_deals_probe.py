"""בדיקות offline ל-deals_probe.py. הרצה: python3 -m unittest discover -s tests"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import deals_probe as dp  # noqa: E402


class DealsProbeTests(unittest.TestCase):
    def test_html_to_text_drops_scripts_and_keeps_lines(self):
        raw = "<html><script>evil()</script><h2>/api/deals/search</h2><p>מסנן &amp; עוד</p><style>x{}</style></html>"
        t = dp.html_to_text(raw)
        self.assertNotIn("evil", t)
        self.assertNotIn("x{}", t)
        self.assertIn("/api/deals/search", t)
        self.assertIn("מסנן & עוד", t)
        self.assertEqual(len(t.splitlines()), 2)

    def test_api_paths_unique_and_clean(self):
        t = "/api/deals/search?settlement=x /api/deals/search /api/deals/parcel/{gush}/{helka}. /api/michrazim/lots"
        self.assertEqual(dp.api_paths(t), ["/api/deals/parcel/{gush}/{helka}", "/api/deals/search", "/api/michrazim/lots"])

    def test_keys_of_lists(self):
        s = dp.keys_of({"total": 3, "deals": [{"b": 1, "a": 2}]})
        self.assertEqual(s["keys"], ["deals", "total"])
        self.assertEqual(s["lists"]["deals"], {"count": 1, "item_keys": ["a", "b"]})

    def test_clean_strips_control_chars_and_caps(self):
        self.assertEqual(dp.clean("a\x1b\n b" + "x" * 10, 5), "a bxx…")


if __name__ == "__main__":
    unittest.main()
