"""בדיקות offline: הקשחת get_json (במקום post_sql מ-v1.5.4), שמירת נתונים קודמים, קוד יציאה של ההתראה השבועית.
הרצה: python3 -m unittest discover -s tests
"""
import http.client
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import build_dashboard as bd  # noqa: E402
import deals_api as api  # noqa: E402
import notify_failure as nf  # noqa: E402
import weekly_watch as ww  # noqa: E402


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def ok(data=None, **extra):
    return FakeResp(json.dumps(dict({"data": data if data is not None else []}, **extra)).encode())


class GetJsonTests(unittest.TestCase):
    """הקשחת ה-HTTP (באגים 2, 5, 6 מ-v1.5.4), עכשיו ב-deals_api.get_json."""

    def setUp(self):
        api._deadline = None
        self.sleeps = []
        p = mock.patch.object(api.time, "sleep", side_effect=self.sleeps.append)
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        api._deadline = None

    def run_with(self, effects):
        with mock.patch.object(api.urllib.request, "urlopen", side_effect=effects) as m:
            try:
                return api.get_json("/api/deals/stats"), m.call_count
            except api.ApiError as e:
                return e, m.call_count

    def test_network_and_parse_errors_are_retried(self):
        for err in (http.client.RemoteDisconnected("x"), http.client.IncompleteRead(b""),
                    ConnectionResetError(), urllib.error.URLError("x"), TimeoutError()):
            with self.subTest(err=type(err).__name__):
                res, calls = self.run_with([err, ok([{"a": 1}])])
                self.assertEqual(res["data"], [{"a": 1}])
                self.assertEqual(calls, 2)

    def test_html_instead_of_json_is_retried(self):
        res, _ = self.run_with([FakeResp(b"<html>busy</html>"), ok([])])
        self.assertEqual(res["data"], [])

    def test_exhausted_raises_with_last_error(self):
        res, calls = self.run_with([ConnectionResetError()] * api.ATTEMPTS)
        self.assertIsInstance(res, api.ApiError)
        self.assertIn("ConnectionResetError", str(res))
        self.assertEqual(calls, api.ATTEMPTS)

    def test_no_sleep_after_last_attempt(self):
        self.run_with([urllib.error.URLError("x")] * api.ATTEMPTS)
        self.assertEqual(len(self.sleeps), api.ATTEMPTS - 1)

    def test_client_error_not_retried(self):
        err = urllib.error.HTTPError(api.BASE, 400, "bad", {}, None)
        res, calls = self.run_with([err])
        self.assertIsInstance(res, api.ApiError)
        self.assertEqual(calls, 1)

    def test_deadline_passed_raises_without_request(self):
        api.start_clock(-1)
        res, calls = self.run_with([ok([])])
        self.assertIsInstance(res, api.ApiError)
        self.assertEqual(calls, 0)

    def test_wait_beyond_deadline_raises_instead_of_sleeping(self):
        api.start_clock(5)
        res, calls = self.run_with([urllib.error.URLError("x"), ok([])])
        self.assertIsInstance(res, api.ApiError)
        self.assertEqual(calls, 1)
        self.assertEqual(self.sleeps, [])

    def test_sqlerror_alias(self):
        self.assertIs(bd.SqlError, api.ApiError)


class KeepPreviousTests(unittest.TestCase):
    PREV = {"generated_at": "2026-11-07T11:15:51+00:00",
            "budget": {"rows": [["א", 1, 1, 1, 1, 1, 1]]},
            "monthly": {"months": ["2026-06"], "rows": [], "stale": True, "stale_since": "2026-11-01T00:00:00+00:00"}}

    def test_marks_stale_with_previous_build_time(self):
        out = bd.keep_previous("budget", self.PREV)
        self.assertTrue(out["stale"])
        self.assertEqual(out["stale_since"], "2026-11-07T11:15:51+00:00")
        self.assertNotIn("stale", self.PREV["budget"])  # לא משנה את המקור

    def test_keeps_original_stale_since(self):
        self.assertEqual(bd.keep_previous("monthly", self.PREV)["stale_since"], "2026-11-01T00:00:00+00:00")

    def test_missing_previous_gives_none(self):
        self.assertIsNone(bd.keep_previous("watch", self.PREV))
        self.assertEqual(bd.load_previous("/nonexistent/x.json"), {})

    def test_build_keeps_previous_sections_on_failure(self):
        # באג 4: בעבר כשל של חלק כתב null והלשונית התרוקנה. עכשיו: אין שליפה מוצלחת לחודשים -> budget ו-watch נשמרים
        import tempfile
        from datetime import date
        with tempfile.TemporaryDirectory() as root:
            for m in ("2026-04", "2026-05", "2026-06"):
                api.save_month({"month": m, "n": 900, "source": "sql-legacy",
                                "days": [[m + "-10", "א", 3, 900, 9e8, 5e5, 2e6]],
                                "monthly": [["א", 3, 900, 9e8, 5e5, 2e6]]}, root)
            prev = dict(self.PREV, watch={"cities": []})
            with mock.patch.object(api, "stats", return_value={"last_deal": "2026-06-30", "scraped_at": "x"}), \
                    mock.patch.object(bd, "load_watch", return_value={"cities": ["א"], "pct": 15, "cap": 45, "max_flags": 3}):
                out = bd.build(today=date(2026, 11, 20), prev=prev, root=root,
                               fetch=mock.Mock(side_effect=api.ApiError("x")))
        for k in ("budget", "watch"):
            self.assertTrue(out[k]["stale"], k)
        self.assertEqual(out["anchor"], "2026-06-10")
        self.assertTrue(out["fetch_errors"])


class WeeklyWatchTests(unittest.TestCase):
    def run_main(self, dash, sent):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(ww, "STATE", os.path.join(d, "s.json")), \
                mock.patch.object(ww, "load_json", side_effect=lambda p: dash if p == ww.DASH else None), \
                mock.patch.object(ww, "build_message", return_value="msg"), \
                mock.patch.object(ww, "send_telegram", return_value=sent) as st:
            return ww.main(), st.call_count

    DASH = {"anchor_month": "2026-06", "db_scraped_at": "x", "watch": {"cities": [{"name": "א"}]}}

    def test_failed_send_returns_1(self):
        # באג 10: בעבר ה-workflow היה ירוק גם כשהשליחה נכשלה
        self.assertEqual(self.run_main(self.DASH, False), (1, 1))

    def test_successful_send_returns_0(self):
        self.assertEqual(self.run_main(self.DASH, True), (0, 1))

    def test_stale_watch_is_not_sent(self):
        dash = dict(self.DASH, watch=dict(self.DASH["watch"], stale=True))
        self.assertEqual(self.run_main(dash, True), (0, 0))


class NotifyFailureTests(unittest.TestCase):
    def test_text_includes_only_github_link(self):
        self.assertIn("https://github.com/o/r/actions/runs/1", nf.build_text("נכשל", "https://github.com/o/r/actions/runs/1"))
        self.assertNotIn("evil", nf.build_text("נכשל", "https://evil.example/x"))


if __name__ == "__main__":
    unittest.main()
