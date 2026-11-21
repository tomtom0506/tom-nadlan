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

    def test_duplicates_collects_counts(self):
        from unittest import mock
        answers = {"archive_all": {"result": {"total": 120, "records": [{"deal_date": "10/05/2026", "row_hash": "a", "x": 1}]}},
                   "archive_latest": {"result": {"total": 100}}, "archive_current": {"result": {"total": 100}},
                   "api": {"total": 100, "data": []}, "log": {"items": []}}

        def fake(cid, label, path, params=None):
            key = "log" if cid == "deals_log" else cid.rsplit("_", 1)[-1]
            key = {"all": "archive_all", "latest": "archive_latest", "current": "archive_current"}.get(key, key)
            return {"ok": True, "status": 200}, answers[key]
        with mock.patch.object(dp, "check", side_effect=fake):
            out, day, log = dp.duplicates()
        self.assertEqual(out[0], {"day": "2026-05-10", "archive_all": 120, "archive_latest": 100,
                                  "archive_current": 100, "api_total": 100})
        self.assertEqual(day["records"][0]["row_hash"], "a")
        self.assertNotIn("x", day["records"][0])
        self.assertEqual(log, {"items": []})


if __name__ == "__main__":
    unittest.main()
