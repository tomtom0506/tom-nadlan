"""בדיקות offline ל-v1.5.6: פירוט שגיאת 4xx בלוג (עכשיו ב-deals_api.get_json).
הרצה: python3 -m unittest discover -s tests
"""
import io
import os
import sys
import unittest
import urllib.error
from email.message import Message
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import deals_api as bd  # noqa: E402


def http_error(code, body=b"", auth=None):
    h = Message()
    if auth:
        h["WWW-Authenticate"] = auth
    return urllib.error.HTTPError(bd.BASE, code, "x", h, io.BytesIO(body))


class ErrorDetailTests(unittest.TestCase):
    def setUp(self):
        bd._deadline = None

    def call(self, err):
        with mock.patch.object(bd.urllib.request, "urlopen", side_effect=[err]), \
                mock.patch.object(bd.time, "sleep"):
            with self.assertRaises(bd.ApiError) as cm:
                bd.get_json("/api/deals/stats")
        return str(cm.exception)

    def test_401_shows_server_reason(self):
        # 401 מאז 08.11: עד עכשיו הלוג הראה רק "HTTP 401" בלי הסיבה
        msg = self.call(http_error(401, b'{"error":"API key required"}', auth='Bearer realm="api"'))
        self.assertTrue(msg.startswith("HTTP 401: "))
        self.assertIn('auth=Bearer realm="api"', msg)
        self.assertIn("API key required", msg)

    def test_no_body_keeps_short_message(self):
        self.assertEqual(self.call(http_error(403)), "HTTP 403")

    def test_control_chars_removed_and_length_capped(self):
        msg = self.call(http_error(400, b"a\x1b[31m\r\nb" + b"x" * 5000))
        self.assertNotIn("\x1b", msg)
        self.assertNotIn("\n", msg)
        self.assertLessEqual(len(msg), len("HTTP 400: ") + bd.DETAIL_MAX + 1)


if __name__ == "__main__":
    unittest.main()
