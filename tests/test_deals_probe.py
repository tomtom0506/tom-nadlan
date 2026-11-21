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

    def test_total_of_both_envelopes(self):
        self.assertEqual(dp.total_of({"success": True, "result": {"total": 7}}), 7)
        self.assertEqual(dp.total_of({"total": 5, "data": []}), 5)
        self.assertIsNone(dp.total_of(None))

    def test_compare_days_pages_and_archive(self):
        from unittest import mock
        api_pages = {"0": {"total": 250, "data": [{"date": "2026-05-10", "amount": i, "junk": 1} for i in range(200)]},
                     "200": {"total": 250, "data": [{"date": "2026-05-10", "amount": i} for i in range(50)]}}

        def fake(cid, label, path, params=None):
            if "datastore_search" in path:
                return {"ok": True, "status": 200}, {"result": {"total": 2, "records": [{"deal_date": "10/05/2026", "x": 9}]}}
            return {"ok": True, "status": 200}, api_pages[params["offset"]]
        with mock.patch.object(dp, "check", side_effect=fake):
            out = dp.compare_days()
        self.assertEqual(len(out), len(dp.CMP_DAYS) * len(dp.APT_NATURES))
        r = out[0]
        self.assertEqual((r["api_total"], len(r["api_rows"]), r["archive_total"]), (250, 250, 2))
        self.assertNotIn("junk", r["api_rows"][0])
        self.assertNotIn("x", r["archive_rows"][0])

if __name__ == "__main__":
    unittest.main()
