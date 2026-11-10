"""Model-list hot reload in a running LiteLLM proxy (offline)."""
import asyncio
import json
import os
from pathlib import Path
import unittest
from unittest import mock

from helpers import TempHome, catalog, run_python

from copilot_gateway import catalog as catalog_mod, gateway, hot_reload, services

TESTS = Path(__file__).resolve().parent


class FakeRouter:
    def __init__(self):
        self.model_list, self.aliases = None, None

    def set_model_list(self, model_list):
        self.model_list = model_list

    def update_settings(self, **kwargs):
        self.aliases = kwargs["model_group_alias"]


class WatcherTests(unittest.TestCase):
    def setUp(self):
        self.home = TempHome()
        self.addCleanup(self.home.cleanup)
        self.settings = self.home.settings()
        gateway.refresh(self.settings, catalog())
        self.path = self.settings.claude_config
        self.state = self.settings.run / "claude.loaded"
        self.router = FakeRouter()
        self.watcher = hot_reload.Watcher(self.path, self.state, self.path.read_text(encoding="utf-8"),
                                          lambda: self.router)

    def record(self):
        return json.loads(self.state.read_text(encoding="utf-8"))

    def test_unchanged_file_records_the_loaded_config(self):
        self.assertFalse(self.watcher.check(first=True))
        self.assertIsNone(self.router.model_list)
        self.assertEqual(self.record(), {"pid": os.getpid(), "needs_restart": None,
                                         "applied": hot_reload.digest(self.path.read_text(encoding="utf-8"))})

    def test_model_list_change_is_applied_in_place(self):
        text = catalog_mod.render(catalog_mod.build_config(catalog(), self.settings.capi_base, {"claude-opus-5"}))
        self.path.write_text(text, encoding="utf-8")
        self.assertTrue(self.watcher.check())
        names = [row["model_name"] for row in self.router.model_list]
        self.assertNotIn("claude-opus-5", names)
        self.assertIn("claude-sonnet-5", names)
        self.assertNotIn("claude-opus-5[1m]", self.router.aliases)
        self.assertIn("claude-haiku-4-5", self.router.aliases)
        self.assertEqual(self.record()["applied"], hot_reload.digest(text))

    def test_other_setting_changes_ask_for_a_restart(self):
        config = catalog_mod.parse_rendered(self.path.read_text(encoding="utf-8"))
        config["litellm_settings"]["callbacks"] = ["x"]
        text = catalog_mod.render(config)
        self.path.write_text(text, encoding="utf-8")
        self.assertFalse(self.watcher.check())
        self.assertIsNone(self.router.model_list)
        self.assertEqual(self.record()["needs_restart"], hot_reload.digest(text))

    def test_invalid_file_keeps_the_previous_list(self):
        self.path.write_text("# header\n{broken", encoding="utf-8")
        self.assertFalse(self.watcher.check())
        self.assertIsNone(self.router.model_list)
        self.assertFalse(self.state.exists())

    def test_catalog_refresh_runs_on_its_interval(self):
        now = [0.0]
        calls = []
        watcher = hot_reload.Watcher(self.path, self.state, self.path.read_text(encoding="utf-8"),
                                     lambda: self.router, refresh=lambda: calls.append(1), refresh_seconds=60,
                                     clock=lambda: now[0])
        asyncio.run(watcher.maybe_refresh())
        self.assertEqual(calls, [])
        now[0] = 61
        asyncio.run(watcher.maybe_refresh())
        asyncio.run(watcher.maybe_refresh())
        self.assertEqual(calls, [1])

    def test_refresh_failure_does_not_stop_the_watcher(self):
        def boom():
            raise catalog_mod.CatalogError("offline")
        watcher = hot_reload.Watcher(self.path, self.state, self.path.read_text(encoding="utf-8"),
                                     lambda: self.router, refresh=boom, refresh_seconds=1, clock=lambda: 5.0)
        watcher.next_refresh = 0
        asyncio.run(watcher.maybe_refresh())


class EnsureTests(unittest.TestCase):
    def setUp(self):
        self.home = TempHome()
        self.addCleanup(self.home.cleanup)
        self.settings = self.home.settings()
        gateway.refresh(self.settings, catalog())
        self.service = gateway.build_services(self.settings, "k")["claude"]
        proc = mock.Mock(pid=4321)
        self.enterContext(mock.patch.object(gateway.services, "inspect",
                                            return_value=services.State("running", [proc])))
        self.want = hot_reload.digest(self.settings.claude_config.read_text(encoding="utf-8"))

    def write(self, **record):
        path = gateway.reload_state(self.settings, "claude")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"pid": 4321, "applied": None, "needs_restart": None, **record}), encoding="utf-8")

    def test_applied_config_needs_no_restart(self):
        self.write(applied=self.want)
        self.assertTrue(gateway.hot_reloaded(self.settings, self.service, timeout=0))

    def test_old_process_or_restart_request_falls_back_to_restart(self):
        self.assertFalse(gateway.hot_reloaded(self.settings, self.service, timeout=0))
        self.write(applied=self.want, pid=1)
        self.assertFalse(gateway.hot_reloaded(self.settings, self.service, timeout=0))
        self.write(applied="old", needs_restart=self.want)
        self.assertFalse(gateway.hot_reloaded(self.settings, self.service, timeout=5))
        self.write(applied="old")
        self.assertFalse(gateway.hot_reloaded(self.settings, self.service, timeout=0))

    def test_runner_command_enables_hot_reload(self):
        command = self.service.command()
        self.assertEqual(command[command.index("--state") + 1], str(gateway.reload_state(self.settings, "claude")))
        self.assertEqual(command[command.index("--refresh-minutes") + 1], "60")
        self.assertEqual(self.service.classify(command), "ours")
        self.assertEqual(self.service.env["CCGW_HOME"], str(self.settings.home))


class LiteLLMRouterTests(unittest.TestCase):
    def test_real_router_and_proxy_lifespan(self):
        result = run_python(r'''
import asyncio, json, sys, tempfile
from pathlib import Path
sys.path.insert(0, TESTS)
from helpers import catalog
from copilot_gateway import catalog as c, hot_reload, litellm_patches
litellm_patches.install()
import litellm
from litellm.proxy import proxy_server
full = c.build_config(catalog())
router = litellm.Router(model_list=full["model_list"], model_group_alias=full["router_settings"]["model_group_alias"])
before = set(router.get_model_names())
hot_reload.apply(router, c.build_config(catalog(), exclude={"claude-opus-5"}))
after = set(router.get_model_names())
original = proxy_server.app.router.lifespan_context
tmp = Path(tempfile.mkdtemp())
(tmp / "c.yaml").write_text(c.render(full), encoding="utf-8")
w = hot_reload.Watcher(tmp / "c.yaml", tmp / "s", c.render(full), lambda: router)
hot_reload.install(proxy_server.app, w)
print(json.dumps({"removed": sorted(before - after), "kept": "claude-sonnet-5" in after,
                  "alias": "claude-haiku-4-5" in router.model_group_alias,
                  "opus_alias": "claude-opus-5[1m]" in router.model_group_alias,
                  "wrapped": proxy_server.app.router.lifespan_context is not original}))
'''.replace("TESTS", repr(str(TESTS))))
        self.assertEqual(result.returncode, 0, result.stderr[-3000:])
        value = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(value, {"removed": ["claude-opus-5"], "kept": True, "alias": True,
                                 "opus_alias": False, "wrapped": True})


if __name__ == "__main__":
    unittest.main()
