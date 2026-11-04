"""The single Copilot login: storage, fingerprint, device flow, `ccgw login` (offline, no real tokens)."""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import unittest
from unittest import mock

from helpers import TempHome

from copilot_gateway import auth, cli


class TokenStoreTests(unittest.TestCase):
    def setUp(self):
        self.home = TempHome()
        self.addCleanup(self.home.cleanup)
        self.dir = self.home.path / "tok"

    def write(self, text, directory=None):
        directory = directory or self.dir
        directory.mkdir(parents=True, exist_ok=True)
        (directory / auth.TOKEN_FILE).write_text(text, encoding="utf-8")

    def test_missing_or_empty_login_is_reported(self):
        for text in (None, "", "  \n"):
            with self.subTest(text=text):
                if text is not None:
                    self.write(text)
                with self.assertRaisesRegex(auth.AuthError, "ccgw login"):
                    auth.read_token(self.dir)
        self.write(" gho_test \n")
        self.assertEqual(auth.read_token(self.dir), "gho_test")

    def test_fingerprint_tracks_token_and_location_without_revealing_it(self):
        self.write("gho_first")
        first = auth.fingerprint(self.dir)
        self.assertEqual(auth.fingerprint(self.dir), first)
        self.assertNotIn("gho_first", first)
        self.write("gho_second")
        self.assertNotEqual(auth.fingerprint(self.dir), first)
        other = self.home.path / "other"
        self.write("gho_second", other)
        self.assertNotEqual(auth.fingerprint(other), auth.fingerprint(self.dir))

    @unittest.skipUnless(os.name == "posix", "POSIX permissions")
    def test_secure_makes_token_files_owner_only(self):
        self.write("gho_test")
        (self.dir / auth.API_KEY_FILE).write_text("{}", encoding="utf-8")
        for name in (auth.TOKEN_FILE, auth.API_KEY_FILE):
            (self.dir / name).chmod(0o644)
        auth.secure(self.dir)
        for name in (auth.TOKEN_FILE, auth.API_KEY_FILE):
            self.assertEqual((self.dir / name).stat().st_mode & 0o777, 0o600)
        auth.secure(self.home.path / "missing")


class DeviceFlowTests(unittest.TestCase):
    CODE = {"device_code": "dev", "user_code": "ABCD-1234", "verification_uri": "https://github.com/login/device",
            "interval": 5, "expires_in": 900}

    def fake(self, replies):
        calls = []

        def request(url, token=None, body=None):
            calls.append((url, body))
            if url == auth.DEVICE_CODE_URL:
                return dict(self.CODE)
            return replies.pop(0)
        return request, calls

    def test_waits_for_approval_and_honours_slow_down(self):
        request, calls = self.fake([{"error": "authorization_pending"}, {"error": "slow_down"},
                                    {"access_token": "ghu_new"}])
        sleeps, prompts = [], []
        with mock.patch.object(auth, "_request", side_effect=request):
            token = auth.device_login(prompt=prompts.append, sleep=sleeps.append, clock=lambda: 0)
        self.assertEqual(token, "ghu_new")
        self.assertEqual(sleeps, [5, 5, 10])
        self.assertIn("ABCD-1234", prompts[0])
        self.assertIn("https://github.com/login/device", prompts[0])
        self.assertEqual(calls[0][1], {"client_id": auth.CLIENT_ID, "scope": auth.SCOPE})
        self.assertEqual(calls[1][1]["grant_type"], "urn:ietf:params:oauth:grant-type:device_code")

    def test_expiry_denial_and_bad_responses_fail(self):
        ticks = iter(range(0, 10000, 300))
        request, _ = self.fake([{"error": "authorization_pending"}] * 10)
        with mock.patch.object(auth, "_request", side_effect=request), \
                self.assertRaisesRegex(auth.AuthError, "expired"):
            auth.device_login(prompt=lambda _: None, sleep=lambda _: None, clock=lambda: next(ticks))
        request, _ = self.fake([{"error": "access_denied"}])
        with mock.patch.object(auth, "_request", side_effect=request), \
                self.assertRaisesRegex(auth.AuthError, "access_denied"):
            auth.device_login(prompt=lambda _: None, sleep=lambda _: None, clock=lambda: 0)
        with mock.patch.object(auth, "_request", return_value={"device_code": "dev"}), \
                self.assertRaisesRegex(auth.AuthError, "user_code"):
            auth.device_login(prompt=lambda _: None, sleep=lambda _: None, clock=lambda: 0)


class LoginTests(unittest.TestCase):
    def setUp(self):
        self.home = TempHome()
        self.addCleanup(self.home.cleanup)
        self.dir = self.home.path / "new" / "tok"
        self.key = {"token": "tid=x", "expires_at": 4102444800, "endpoints": {"api": "https://api.example"}}

    def test_new_login_is_stored_private_with_its_own_copilot_key(self):
        with mock.patch.object(auth, "copilot_key", return_value=self.key), \
                mock.patch.object(auth, "github_login", return_value="someone"):
            self.assertEqual(auth.login(self.dir, flow=lambda prompt: "ghu_new"), "someone")
        self.assertEqual(auth.read_token(self.dir), "ghu_new")
        self.assertEqual(json.loads((self.dir / auth.API_KEY_FILE).read_text(encoding="utf-8")), self.key)
        if os.name == "posix":
            self.assertEqual(self.dir.stat().st_mode & 0o777, 0o700)
            for name in (auth.TOKEN_FILE, auth.API_KEY_FILE):
                self.assertEqual((self.dir / name).stat().st_mode & 0o777, 0o600)

    def test_login_without_copilot_access_keeps_the_previous_one(self):
        self.dir.mkdir(parents=True)
        (self.dir / auth.TOKEN_FILE).write_text("ghu_old", encoding="utf-8")
        with mock.patch.object(auth, "copilot_key", side_effect=auth.AuthError("no Copilot")), \
                self.assertRaises(auth.AuthError):
            auth.login(self.dir, flow=lambda prompt: "ghu_new")
        self.assertEqual(auth.read_token(self.dir), "ghu_old")
        self.assertFalse((self.dir / auth.API_KEY_FILE).exists())


class TokenDirSettingsTests(unittest.TestCase):
    def setUp(self):
        self.home = TempHome()
        self.addCleanup(self.home.cleanup)
        del self.home.environ["GITHUB_COPILOT_TOKEN_DIR"]

    def test_env_file_injects_token_dir_and_process_env_wins(self):
        injected = self.home.path / "accounts" / "personal"
        (self.home.path / ".env").write_text(f'GITHUB_COPILOT_TOKEN_DIR="{injected}"\n', encoding="utf-8")
        self.assertEqual(self.home.settings().token_dir, injected)
        override = self.home.path / "accounts" / "other"
        self.home.environ["GITHUB_COPILOT_TOKEN_DIR"] = str(override)
        self.assertEqual(self.home.settings().token_dir, override)

    def test_default_and_relative_paths(self):
        self.assertEqual(self.home.settings().token_dir, Path.home() / ".config" / "litellm" / "github_copilot")
        self.home.environ["GITHUB_COPILOT_TOKEN_DIR"] = "relative/tok"
        with self.assertRaises(SystemExit):
            self.home.settings()


class LoginCommandTests(unittest.TestCase):
    def setUp(self):
        self.home = TempHome()
        self.addCleanup(self.home.cleanup)
        self.settings = self.home.settings()
        self.enterContext(mock.patch.object(cli, "account_label", return_value="someone (plan)"))
        self.login = self.enterContext(mock.patch.object(cli.auth, "login", return_value="someone"))
        self.refresh = self.enterContext(mock.patch.object(cli.gateway, "refresh"))
        self.apply = self.enterContext(mock.patch.object(cli.gateway, "apply_login", return_value=["capi"]))

    def run_login(self, force=False):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(cli.cmd_login(self.settings, argparse.Namespace(force=force)), 0)
        return output.getvalue()

    def test_first_login_stores_without_touching_services(self):
        self.assertIn("for someone stored", self.run_login())
        self.login.assert_called_once()
        self.apply.assert_not_called()

    def test_existing_login_is_kept_unless_forced(self):
        self.settings.token_dir.mkdir(parents=True)
        (self.settings.token_dir / auth.TOKEN_FILE).write_text("ghu_old", encoding="utf-8")
        output = self.run_login()
        self.assertIn("Already logged in", output)
        self.assertIn("someone (plan)", output)
        self.assertNotIn("ghu_old", output)
        self.login.assert_not_called()
        self.assertIn("now use the new login", self.run_login(force=True))
        self.login.assert_called_once()
        self.refresh.assert_called_once_with(self.settings)
        self.apply.assert_called_once()


if __name__ == "__main__":
    unittest.main()
