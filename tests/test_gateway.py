"""Gateway services always run the current login (offline; service control is mocked)."""
import os
import unittest
from unittest import mock

from helpers import TempHome, catalog

from copilot_gateway import auth, copilot_api, gateway, services


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.home = TempHome()
        self.addCleanup(self.home.cleanup)
        self.settings = self.home.settings()
        self.settings.token_dir.mkdir(parents=True)
        self.set_token("gho_first")
        gateway.refresh(self.settings, catalog())
        self.status = {}
        self.enterContext(mock.patch.object(gateway.copilot_api, "ensure_installed"))
        self.enterContext(mock.patch.object(gateway, "sync_web_search", return_value=False))
        self.enterContext(mock.patch.object(gateway.services, "check_ready", return_value=6))
        self.enterContext(mock.patch.object(
            gateway.services, "inspect", side_effect=lambda svc: services.State(self.status.get(svc.name, "stopped"), [])))
        self.stop = self.enterContext(mock.patch.object(
            gateway.services, "stop", side_effect=lambda svc, log=None: self.status.update({svc.name: "stopped"})))
        self.start = self.enterContext(mock.patch.object(gateway.services, "start", side_effect=self.fake_start))

    def fake_start(self, svc, log=None):
        if self.status.get(svc.name) == "running":
            return False
        self.status[svc.name] = "running"
        return True

    def set_token(self, value):
        (self.settings.token_dir / auth.TOKEN_FILE).write_text(value, encoding="utf-8")

    def ensure(self):
        gateway.ensure(self.settings, ["claude"], do_refresh=False, log=lambda *_: None)

    def stopped(self):
        return sorted(call.args[0].name for call in self.stop.call_args_list)

    def test_started_services_record_the_login_and_are_reused(self):
        self.ensure()
        expected = auth.fingerprint(self.settings.token_dir)
        for name in ("capi", "claude"):
            self.assertEqual(gateway.credential_file(self.settings, name).read_text(encoding="utf-8").strip(), expected)
        self.ensure()
        self.assertEqual(self.stopped(), [])

    def test_a_new_login_restarts_running_services(self):
        self.ensure()
        self.set_token("gho_second")
        self.ensure()
        self.assertEqual(self.stopped(), ["capi", "claude"])
        self.assertEqual(gateway.credential_file(self.settings, "capi").read_text(encoding="utf-8").strip(),
                         auth.fingerprint(self.settings.token_dir))

    def test_running_service_with_unknown_login_is_restarted(self):
        self.status.update(capi="running", claude="running")
        self.ensure()
        self.assertEqual(self.stopped(), ["capi", "claude"])

    def test_no_login_starts_nothing(self):
        (self.settings.token_dir / auth.TOKEN_FILE).unlink()
        with self.assertRaisesRegex(auth.AuthError, "ccgw login"):
            self.ensure()
        self.start.assert_not_called()

    def test_capi_gets_the_gateway_login_only_at_spawn(self):
        table = gateway.build_services(self.settings, "k")
        self.assertEqual(table["capi"].secret_env(), {copilot_api.TOKEN_ENV: "gho_first"})
        self.assertNotIn(copilot_api.TOKEN_ENV, table["capi"].env)
        for name in ("claude", "codex"):
            env = table[name].env
            self.assertEqual(env["GITHUB_COPILOT_TOKEN_DIR"], str(self.settings.token_dir))
            self.assertEqual((env["GITHUB_COPILOT_ACCESS_TOKEN_FILE"], env["GITHUB_COPILOT_API_KEY_FILE"]),
                             (auth.TOKEN_FILE, auth.API_KEY_FILE))
            self.assertIsNone(table[name].secret_env)
        (self.settings.token_dir / auth.TOKEN_FILE).unlink()
        gateway.build_services(self.settings, "k")  # status/stop must work without a login

    def test_apply_login_moves_only_running_services(self):
        with mock.patch.object(gateway, "ensure") as ensure:
            self.assertEqual(gateway.apply_login(self.settings), [])
            ensure.assert_not_called()
            self.status.update(capi="running", codex="running")
            self.assertEqual(gateway.apply_login(self.settings), ["capi", "codex"])
            ensure.assert_called_once_with(self.settings, ["codex"], do_refresh=False, log=mock.ANY)


class SpawnEnvTests(unittest.TestCase):
    def test_secret_env_is_added_only_to_the_child(self):
        home = TempHome()
        self.addCleanup(home.cleanup)
        service = services.Service("capi", 24141, home.path / "capi.log", "capi", command=lambda: ["node", "x"],
                                   env={"HOST": "127.0.0.1"}, secret_env=lambda: {copilot_api.TOKEN_ENV: "tok"})
        with mock.patch.dict(os.environ, {copilot_api.TOKEN_ENV: "inherited"}), \
                mock.patch.object(services.subprocess, "Popen") as popen:
            services._spawn(service)
            self.assertEqual(os.environ[copilot_api.TOKEN_ENV], "inherited")
        env = popen.call_args.kwargs["env"]
        self.assertEqual((env[copilot_api.TOKEN_ENV], env["HOST"]), ("tok", "127.0.0.1"))


if __name__ == "__main__":
    unittest.main()
