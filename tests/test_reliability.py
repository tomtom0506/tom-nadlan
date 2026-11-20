"""בדיקות offline ל-v1.5.4: הקשחת post_sql, שמירת נתונים קודמים, קוד יציאה של ההתראה השבועית.
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
import notify_failure as nf  # noqa: E402
import weekly_watch as ww  # noqa: E402


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def ok(rows, **extra):
    return FakeResp(json.dumps(dict({"rows": rows}, **extra)).encode())


class PostSqlTests(unittest.TestCase):
    def setUp(self):
        bd._deadline = None
        self.sleeps = []
        p = mock.patch.object(bd.time, "sleep", side_effect=self.sleeps.append)
        p.start()
        self.addCleanup(p.stop)

    def run_with(self, effects):
        with mock.patch.object(bd.urllib.request, "urlopen", side_effect=effects) as m:
            try:
                return bd.post_sql("SELECT 1"), m.call_count
            except bd.SqlError as e:
                return e, m.call_count

    def test_network_and_parse_errors_are_retried(self):
        # באג 2: אלה יצאו בעבר כחריגה לא מטופלת והפילו את כל הבנייה
        for err in (http.client.RemoteDisconnected("x"), http.client.IncompleteRead(b""),
                    ConnectionResetError(), urllib.error.URLError("x"), TimeoutError()):
            with self.subTest(err=type(err).__name__):
                res, calls = self.run_with([err, ok([{"a": 1}])])
                self.assertEqual(res, [{"a": 1}])
                self.assertEqual(calls, 2)

    def test_html_instead_of_json_is_retried(self):
        res, _ = self.run_with([FakeResp(b"<html>busy</html>"), ok([])])
        self.assertEqual(res, [])

    def test_exhausted_raises_sqlerror_with_last_error(self):
        res, calls = self.run_with([ConnectionResetError()] * bd.ATTEMPTS)
        self.assertIsInstance(res, bd.SqlError)
        self.assertIn("ConnectionResetError", str(res))
        self.assertEqual(calls, bd.ATTEMPTS)

    def test_no_sleep_after_last_attempt(self):
        # באג 5: בעבר הייתה השהיה גם אחרי הניסיון האחרון
        self.run_with([urllib.error.URLError("x")] * bd.ATTEMPTS)
        self.assertEqual(len(self.sleeps), bd.ATTEMPTS - 1)

    def test_client_error_not_retried(self):
        err = urllib.error.HTTPError(bd.SQL_URL, 400, "bad", {}, None)
        res, calls = self.run_with([err])
        self.assertIsInstance(res, bd.SqlError)
        self.assertEqual(calls, 1)

    def test_truncated_response_raises(self):
        # באג 6: דף חתוך היה מסיים את ה-pagination בשקט
        res, _ = self.run_with([ok([{"a": 1}], truncated=True)])
        self.assertIsInstance(res, bd.SqlError)
        self.assertIn("Truncated", str(res))

    def test_deadline_passed_raises_without_request(self):
        bd.start_clock(-1)
        res, calls = self.run_with([ok([])])
        self.assertIsInstance(res, bd.SqlError)
        self.assertEqual(calls, 0)

    def test_wait_beyond_deadline_raises_instead_of_sleeping(self):
        bd.start_clock(5)
        res, calls = self.run_with([urllib.error.URLError("x"), ok([])])
        self.assertIsInstance(res, bd.SqlError)
        self.assertEqual(calls, 1)
        self.assertEqual(self.sleeps, [])

    def tearDown(self):
        bd._deadline = None


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
        # באג 4: בעבר כשל של חלק כתב null והלשונית התרוקנה
        fail = mock.Mock(side_effect=bd.SqlError("x"))
        with mock.patch.multiple(bd, month_counts=mock.Mock(return_value={"2026-05": 900, "2026-06": 900}),
                                 fetch_detail=mock.Mock(return_value=[]), fetch_budget=fail, fetch_monthly=fail,
                                 fetch_watch=fail, load_watch=mock.Mock(return_value={"cities": ["א"]}),
                                 freshness=mock.Mock(return_value={})):
            prev = dict(self.PREV, watch={"cities": []})
            out = bd.build(today=bd.date(2026, 11, 20), prev=prev)
        for k in ("budget", "monthly", "watch"):
            self.assertTrue(out[k]["stale"], k)


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
