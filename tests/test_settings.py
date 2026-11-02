"""Run inside the container: python -m unittest discover -s tests"""
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())

import app as pies  # noqa: E402

HEADERS = {"X-Requested-With": "PiesWatch"}


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.saved = dict(pies.settings)
        pies.SETTINGS_FILE.unlink(missing_ok=True)

    def tearDown(self):
        pies.apply_settings(self.saved)
        pies.SETTINGS_FILE.unlink(missing_ok=True)

    def validate(self, **incoming):
        return pies.validate_settings(pies.default_settings(), incoming, "en")

    def test_rejects_numbers_out_of_range(self):
        values, error = self.validate(bark_threshold="2")
        self.assertIsNone(values)
        self.assertIn("0.05", error)

    def test_accepts_decimal_comma(self):
        values, error = self.validate(bark_threshold="0,4", bark_high_threshold="0,6")
        self.assertIsNone(error)
        self.assertEqual(values["bark_threshold"], 0.4)

    def test_high_threshold_not_below_keep_threshold(self):
        _, error = self.validate(bark_threshold="0.5", bark_high_threshold="0.4")
        self.assertIn("high score", error)

    def test_url_source_needs_known_scheme(self):
        _, error = self.validate(source_type="url", source_url="file:///etc/passwd")
        self.assertIsNotNone(error)
        values, error = self.validate(source_type="url", source_url="rtsp://10.0.0.5/live")
        self.assertIsNone(error)

    def test_empty_password_keeps_stored_one(self):
        current = dict(pies.default_settings(), source_password="old-secret")
        values, _ = pies.validate_settings(current, {"source_password": ""}, "pl")
        self.assertEqual(values["source_password"], "old-secret")

    def test_login_needs_long_password(self):
        _, error = self.validate(auth_enabled=True, auth_user="anna", auth_password="short")
        self.assertIn("8", error)
        values, error = self.validate(auth_enabled=True, auth_user="anna", auth_password="long enough")
        self.assertIsNone(error)
        self.assertTrue(values["auth_password_hash"].startswith(("scrypt", "pbkdf2")))

    def test_credentials_go_into_url_and_are_hidden_in_errors(self):
        url = pies.with_credentials("rtsp://cam.local:554/s1", "user", "p@ss word")
        self.assertEqual(url, "rtsp://user:p%40ss%20word@cam.local:554/s1")
        self.assertEqual(pies.with_credentials("rtsp://a:b@cam/s", "x", "y"), "rtsp://a:b@cam/s")
        values = dict(pies.default_settings(), source_type="url", source_url="rtsp://cam.local:554/s1",
                      source_user="user", source_password="p@ss word")
        pies.apply_settings(values)
        message = pies.hide_secrets(f"failed to open {url} with p@ss word")
        self.assertNotIn("p@ss", message)
        self.assertNotIn("p%40ss", message)

    def test_microphone_source_uses_alsa(self):
        pies.apply_settings(dict(pies.default_settings(), source_type="microphone", alsa_device="hw:1,0"))
        self.assertEqual(pies.source_input(), ["-f", "alsa", "-i", "hw:1,0"])
        self.assertIsNone(pies.preview_source())

    def test_source_change_bumps_version(self):
        before = pies.source_version
        pies.apply_settings(dict(pies.settings, dahua_host="10.1.1.1", source_type="dahua"))
        self.assertGreater(pies.source_version, before)
        same = pies.source_version
        pies.apply_settings(dict(pies.settings, bark_padding_seconds=4))
        self.assertEqual(pies.source_version, same)

    def test_set_login_command_writes_hash_only(self):
        with mock.patch("sys.stdin", io.StringIO("correct horse\n")), mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(pies.command_line(["--set-login", "anna"]), 0)
        stored = pies.load_settings()
        self.assertTrue(stored["auth_enabled"])
        self.assertNotIn("correct horse", pies.SETTINGS_FILE.read_text())
        self.assertTrue(pies.check_password_hash(stored["auth_password_hash"], "correct horse"))


class AccessTest(unittest.TestCase):
    def setUp(self):
        self.saved = dict(pies.settings)
        self.client = pies.app.test_client()

    def tearDown(self):
        pies.apply_settings(self.saved)
        pies.SETTINGS_FILE.unlink(missing_ok=True)

    def enable_login(self):
        values, error = pies.validate_settings(pies.settings, {"auth_enabled": True, "auth_user": "anna",
                                                                "auth_password": "correct horse"}, "pl")
        self.assertIsNone(error)
        pies.apply_settings(values)

    def test_open_panel_without_login(self):
        pies.apply_settings(dict(pies.settings, auth_enabled=False))
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertIn(b"hub.mateopoznan.pl", self.client.get("/").data)

    def test_changes_need_custom_header(self):
        pies.apply_settings(dict(pies.settings, auth_enabled=False))
        self.assertEqual(self.client.post("/api/settings", json={"place_name": "x"}).status_code, 403)
        response = self.client.post("/api/settings", json={"place_name": "Garden"}, headers=HEADERS)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["place_name"], "Garden")
        self.assertNotIn("source_password", response.get_json())

    def test_proxy_without_login_is_blocked(self):
        pies.apply_settings(dict(pies.settings, auth_enabled=False))
        response = self.client.get("/", headers={"X-Forwarded-For": "203.0.113.9"})
        self.assertEqual(response.status_code, 403)

    def test_login_flow(self):
        self.enable_login()
        self.assertEqual(self.client.get("/api/status").status_code, 401)
        self.assertEqual(self.client.get("/").status_code, 302)
        self.assertEqual(self.client.get("/static/app.css").status_code, 200)
        bad = self.client.post("/login", data={"user": "anna", "password": "wrong"})
        self.assertEqual(bad.status_code, 401)
        good = self.client.post("/login", data={"user": "anna", "password": "correct horse", "next": "/settings"})
        self.assertEqual(good.status_code, 302)
        self.assertTrue(good.headers["Location"].endswith("/settings"))
        self.assertEqual(self.client.get("/api/status").status_code, 200)
        self.assertEqual(self.client.get("/", headers={"X-Forwarded-For": "203.0.113.9"}).status_code, 200)

    def test_login_redirect_stays_on_site(self):
        self.enable_login()
        response = self.client.post("/login", data={"user": "anna", "password": "correct horse",
                                                    "next": "//evil.example/"})
        self.assertTrue(response.headers["Location"].endswith("/"))
        self.assertNotIn("evil", response.headers["Location"])

    def test_repeated_failures_lock_login(self):
        self.enable_login()
        pies.login_failures.clear()
        for _ in range(5):
            self.client.post("/login", data={"user": "anna", "password": "wrong"})
        locked = self.client.post("/login", data={"user": "anna", "password": "correct horse"})
        self.assertEqual(locked.status_code, 401)
        pies.login_failures.clear()

    def test_english_csv_headers(self):
        pies.apply_settings(dict(pies.settings, auth_enabled=False))
        self.client.set_cookie("lang", "en")
        body = self.client.get("/download/report.csv").data.decode("utf-8-sig")
        self.assertTrue(body.startswith("started,ended,block_length_s"))
        self.client.set_cookie("lang", "pl")
        body = self.client.get("/download/report.csv").data.decode("utf-8-sig")
        self.assertTrue(body.startswith("started,ended,dlugosc_bloku_s"))


if __name__ == "__main__":
    unittest.main()
