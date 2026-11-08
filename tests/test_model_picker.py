"""Account-scoped picker construction (offline; no real client or provider processes)."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

from helpers import TempHome, catalog, model

from copilot_gateway import catalog as gateway_catalog
from copilot_gateway import gateway, services
from copilot_gateway.clients import claude, codex, codex_rpc, model_picker


def sources():
    value = catalog()
    value["data"] = [row for row in value["data"] if row["id"] != "gpt-6-astra"]
    value["data"].append(model("gpt-6.1-sol", "GPT-6.1 Sol", 1050000, 922000, 128000, ["/responses"],
                               model_picker_category="powerful"))
    config = gateway_catalog.build_codex_config(value)
    normal, native = [], []
    for row in config["model_list"]:
        name = row["model_name"]
        normalized = name.replace(".", "-") if name.startswith("claude-") else name
        normal.append({"id": normalized, "model_picker_enabled": True, "policy": {"state": "enabled"},
                       "capabilities": {"supports": {"vision": name != "gpt-5-mini",
                                                     "reasoning_effort": ["low", "high"]}}})
        native.append({"slug": normalized, "supported_reasoning_levels": [
            {"effort": "low", "description": "Light reasoning"},
            {"effort": "high", "description": "Deep reasoning"}, {"effort": "ultra"}],
                       "default_reasoning_level": "ultra"})
    ghost = {"slug": "gpt-6-astra", "supported_reasoning_levels": [], "default_reasoning_level": None}
    bundled = {"models": [ghost, {**native[-1], "slug": "gpt-6.1-sol"}]}
    return value, config, {"data": normal}, bundled


class ClaudePickerTests(unittest.TestCase):
    def setUp(self):
        self.home = TempHome()
        self.addCleanup(self.home.cleanup)
        self.settings = self.home.settings()
        gateway.refresh(self.settings, catalog())
        self.config = claude.load_config(self.settings)
        self.env = claude.claude_env(self.settings, "key", base={}, config=self.config)

    def test_only_public_account_models_are_present(self):
        picker = model_picker.claude_picker(self.config)
        expected = [row["model_name"] for row in self.config["model_list"]]
        self.assertTrue(picker["replaceBuiltInOptions"])
        self.assertEqual([row["model"] for row in picker["options"]], expected)
        self.assertNotIn("fable", expected)
        self.assertNotIn("gpt-disabled", expected)

    def test_settings_are_merged_privately_and_other_arguments_survive(self):
        original = self.home.path / "original settings.json"
        original.write_text(json.dumps({"fastMode": False, "env": {"SECRET": "private-test-value"},
                                        "permissions": {"defaultMode": "plan"}}), encoding="utf-8")
        destination = self.home.path / "private" / "settings.json"
        args = model_picker.claude_settings_args(
            ["--model", "claude-sonnet-5", "--settings", str(original), "--", "--settings", "prompt"],
            self.config, destination)
        self.assertEqual(args, ["--settings", str(destination), "--model", "claude-sonnet-5",
                                "--", "--settings", "prompt"])
        self.assertNotIn("private-test-value", " ".join(args))
        combined = json.loads(destination.read_text(encoding="utf-8"))
        self.assertFalse(combined["fastMode"])
        self.assertEqual(combined["env"]["SECRET"], "private-test-value")
        self.assertEqual(combined["permissions"]["defaultMode"], "plan")
        self.assertEqual(combined["modelPicker"], model_picker.claude_picker(self.config))
        if os.name == "posix":
            self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("modelPicker", json.loads(original.read_text(encoding="utf-8")))

    def test_last_settings_value_wins_and_malformed_settings_fail(self):
        path = self.home.path / "settings.json"
        args = model_picker.claude_settings_args(
            ['--settings={"fastMode":true}', "--settings", '{"fastMode":false}', "-p", "hello"],
            self.config, path)
        self.assertEqual(args, ["--settings", str(path), "-p", "hello"])
        self.assertFalse(json.loads(path.read_text(encoding="utf-8"))["fastMode"])
        for argv in (["--settings"], ["--settings="], ["--settings", "[]"],
                     ["--settings", "{broken"], ["--settings", str(self.home.path / "missing")]):
            with self.subTest(argv=argv), self.assertRaises(services.ServiceError):
                model_picker.claude_settings_args(argv, self.config, path)

    def test_aliases_resolve_only_to_their_own_offered_model(self):
        self.assertEqual(claude.picker_model("default", self.env, self.config), "claude-sonnet-5")
        self.assertEqual(claude.picker_model("sonnet", self.env, self.config), "claude-sonnet-5")
        self.assertEqual(claude.picker_model("sonnet[1m]", self.env, self.config), "claude-sonnet-5[1m]")
        self.assertEqual(claude.picker_model("claude-haiku-4-5", self.env, self.config), "claude-haiku-4-5")
        for value in ("fable", "claude-opus-9", "gpt-disabled", "sonnet[1m]-other"):
            with self.subTest(value=value), self.assertRaisesRegex(services.ServiceError, "not offered"):
                claude.picker_model(value, self.env, self.config)
        wrong_family = {**self.env, "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-sonnet-5"}
        with self.assertRaisesRegex(services.ServiceError, "not offered"):
            claude.picker_model("opus", wrong_family, self.config)

    def test_explicit_model_parser_honors_the_last_option_and_separator(self):
        self.assertEqual(claude.explicit_model(["--model", "x", "--model=y", "--", "--model=z"]), "y")
        self.assertIsNone(claude.explicit_model(["--", "--model=x"]))
        with self.assertRaises(services.ServiceError):
            claude.explicit_model(["--model", "--settings"])

    def test_opt_in_launch_preserves_settings_permissions_and_cleans_up(self):
        captured = {}

        def fake_run(command, env, *, wait=False):
            path = Path(command[command.index("--settings") + 1])
            captured.update(command=command, env=env, wait=wait, path=path,
                            contents=json.loads(path.read_text(encoding="utf-8")))
            return 0

        with mock.patch.object(gateway, "ensure") as ensure, \
                mock.patch.object(claude.Settings, "load", return_value=self.settings), \
                mock.patch.object(claude.Settings, "master_key", return_value="key"), \
                mock.patch.object(claude.shutil, "which", return_value="/test/claude"), \
                mock.patch.object(model_picker, "require_version") as version, \
                mock.patch.object(claude, "run", side_effect=fake_run), \
                mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(claude.main(["--respect-permissions", "--sync-model-picker",
                                          "--settings", '{"fastMode":false}',
                                          "--permission-mode", "default"]), 0)
        ensure.assert_called_once_with(self.settings, ["claude"], do_refresh=True, require_refresh=True)
        version.assert_called_once_with("/test/claude", "Claude Code", model_picker.CLAUDE_MIN_VERSION)
        self.assertTrue(captured["wait"])
        self.assertNotIn("--dangerously-skip-permissions", captured["command"])
        self.assertIn("--permission-mode", captured["command"])
        self.assertFalse(captured["contents"]["fastMode"])
        self.assertEqual(captured["env"]["ANTHROPIC_DEFAULT_MODEL"], "claude-sonnet-5")
        self.assertFalse(captured["path"].exists())

    def test_opt_in_rejects_an_explicit_unavailable_model(self):
        with mock.patch.object(gateway, "ensure"), \
                mock.patch.object(claude.Settings, "load", return_value=self.settings), \
                mock.patch.object(claude.Settings, "master_key", return_value="key"), \
                mock.patch.object(claude.shutil, "which", return_value="/test/claude"), \
                mock.patch.object(model_picker, "require_version"), \
                mock.patch.object(claude, "run") as run, \
                mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(claude.main(["--sync-model-picker", "--model", "claude-opus-9"]), 1)
            run.assert_not_called()


class CodexPickerTests(unittest.TestCase):
    def setUp(self):
        self.home = TempHome()
        self.addCleanup(self.home.cleanup)
        self.settings = self.home.settings()
        self.value, self.config, self.normal, self.bundled = sources()

    def build(self):
        return model_picker.codex_picker(self.config, self.normal, self.bundled)

    def test_filters_unavailable_models_and_uses_account_capabilities(self):
        before = json.dumps((self.normal, self.bundled), sort_keys=True)
        selected = self.build()
        by_id = {row["id"]: row for row in selected}
        self.assertEqual(set(by_id), {row["model_name"] for row in self.config["model_list"]})
        self.assertNotIn("gpt-6-astra", by_id)
        self.assertIn("claude-haiku-4.5", by_id)
        native = by_id["gpt-6.1-sol"]
        self.assertTrue(native["isDefault"])
        self.assertEqual([row["reasoningEffort"] for row in native["supportedReasoningEfforts"]], ["low", "high"])
        self.assertEqual(native["supportedReasoningEfforts"][0]["description"], "Light reasoning")
        self.assertEqual(native["defaultReasoningEffort"], "low")
        self.assertEqual(native["serviceTiers"], [])
        self.assertEqual(by_id["gpt-5-mini"]["inputModalities"], ["text"])
        self.assertEqual([row["reasoningEffort"] for row in by_id["claude-sonnet-5"]["supportedReasoningEfforts"]],
                         ["low", "high"])
        self.assertEqual(json.dumps((self.normal, self.bundled), sort_keys=True), before)

    def test_incomplete_or_stale_metadata_is_rejected(self):
        for mutation in ("missing", "disabled", "hidden", "capabilities", "reasoning", "duplicate"):
            with self.subTest(mutation=mutation):
                _, config, normal, bundled = sources()
                if mutation == "missing":
                    normal["data"].pop(0)
                elif mutation == "disabled":
                    normal["data"][0]["policy"] = {"state": "disabled"}
                elif mutation == "hidden":
                    normal["data"][0]["model_picker_enabled"] = False
                elif mutation == "capabilities":
                    normal["data"][0]["capabilities"] = {}
                elif mutation == "reasoning":
                    normal["data"][0]["capabilities"]["supports"]["reasoning_effort"] = "high"
                else:
                    normal["data"].append(normal["data"][0])
                with self.assertRaises(services.ServiceError):
                    model_picker.codex_picker(config, normal, bundled)

    def test_default_preserves_an_offered_saved_model_or_same_model_alias(self):
        for saved in ("claude-haiku-4.5", "claude-haiku-4-5"):
            with self.subTest(saved=saved):
                selected = model_picker.codex_picker(self.config, self.normal, self.bundled, saved)
                self.assertEqual([row["id"] for row in selected if row["isDefault"]], ["claude-haiku-4.5"])
        selected = model_picker.codex_picker(self.config, self.normal, self.bundled, "gpt-6-astra")
        self.assertEqual([row["id"] for row in selected if row["isDefault"]], ["gpt-6.1-sol"])

    def test_opt_in_filters_only_app_server_without_changing_codex_config(self):
        gateway.refresh(self.settings, self.value)
        models = self.build()
        args = ["-c", 'model_catalog_json="other.json"', "app-server"]
        with mock.patch.object(gateway, "ensure") as ensure, \
                mock.patch.object(codex.Settings, "load", return_value=self.settings), \
                mock.patch.object(codex.Settings, "master_key", return_value="key"), \
                mock.patch.object(codex, "find_codex", return_value="/test/codex"), \
                mock.patch.object(model_picker, "require_version"), \
                mock.patch.object(model_picker, "load_codex_picker", return_value=models), \
                mock.patch.object(codex_rpc, "run", return_value=0) as run, \
                mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(codex.main(["--sync-model-picker", *args]), 0)
        ensure.assert_called_once_with(self.settings, ["codex"], do_refresh=True, require_refresh=True)
        self.assertEqual(run.call_args.args[0], ["/test/codex", *args])
        self.assertEqual(run.call_args.args[2], models)
        self.assertNotIn("model_catalog_json", (self.settings.codex_home / "config.toml").read_text(encoding="utf-8"))

    def test_retired_saved_model_can_open_picker_without_being_rewritten(self):
        gateway.refresh(self.settings, self.value)
        codex.write_codex_home(self.settings, "gpt-6-astra")
        with mock.patch.object(gateway, "ensure"), \
                mock.patch.object(codex.Settings, "load", return_value=self.settings), \
                mock.patch.object(codex.Settings, "master_key", return_value="key"), \
                mock.patch.object(codex, "find_codex", return_value="/test/codex"), \
                mock.patch.object(model_picker, "require_version"), \
                mock.patch.object(model_picker, "load_codex_picker", return_value=self.build()), \
                mock.patch.object(codex_rpc, "run", return_value=0) as run, \
                mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(codex.main(["--sync-model-picker", "app-server"]), 0)
            self.assertEqual(codex.saved_model(self.settings), "gpt-6-astra")
            run.assert_called_once()
            run.reset_mock()
            self.assertEqual(codex.main(["--sync-model-picker", "-m", "gpt-6-astra", "app-server"]), 1)
            run.assert_not_called()

    def test_unsupported_invocations_fail_before_starting_services(self):
        with mock.patch.object(gateway, "ensure") as ensure:
            for argv in (["--sync-model-picker", "exec"], ["--sync-model-picker", "code"],
                         ["--sync-model-picker", "app-server", "--listen=ws://127.0.0.1:1234"]):
                with self.subTest(argv=argv):
                    self.assertEqual(codex.main(argv), 1)
            ensure.assert_not_called()


class CodexRpcTests(unittest.TestCase):
    def setUp(self):
        self.models = [{"id": "one"}, {"id": "two"}, {"id": "three"}]

    def response(self, request):
        return json.loads(codex_rpc.model_list_response(json.dumps(request).encode(), self.models))

    def test_model_list_paginates_and_preserves_request_id(self):
        first = self.response({"jsonrpc": "2.0", "id": "id-1", "method": "model/list", "params": {"limit": 2}})
        self.assertEqual(first["jsonrpc"], "2.0")
        self.assertEqual(first["id"], "id-1")
        self.assertEqual(first["result"], {"data": self.models[:2], "nextCursor": "ccgw:2"})
        second = self.response({"id": 2, "method": "model/list", "params": {"cursor": "ccgw:2", "limit": 2}})
        self.assertEqual(second["result"], {"data": self.models[2:], "nextCursor": None})
        self.assertEqual(self.response({"id": 3, "method": "model/list"})["result"]["data"], self.models)
        self.assertEqual(self.response({"id": 4, "method": "model/list", "params": {"limit": 0}})["result"],
                         {"data": [], "nextCursor": None})

    def test_invalid_requests_fail_and_other_messages_pass_through(self):
        for params in ({"limit": -1}, {"limit": True}, {"cursor": "other:1"}, {"cursor": "ccgw:no"},
                       {"cursor": "ccgw:4"}, {"includeHidden": "yes"}, []):
            with self.subTest(params=params):
                response = self.response({"id": 1, "method": "model/list", "params": params})
                self.assertEqual(response["error"]["code"], -32602)
        for line in (b"not-json\n", b'{"id":1,"method":"thread/start"}\n',
                     b'{"method":"model/list"}\n', b'{"id":null,"method":"model/list"}\n'):
            self.assertIsNone(codex_rpc.model_list_response(line, self.models))

    def test_only_stdio_app_server_is_supported(self):
        for argv in (["app-server"], ["app-server", "--stdio"], ["app-server", "--listen=stdio://"],
                     ["app-server", "--listen", "stdio://"],
                     ["-c", "approval_policy=never", "-c", "features.plugins=false", "-s", "read-only",
                      "-a", "never", "app-server"]):
            codex_rpc.validate_arguments(argv)
        for argv in ([], ["exec"], ["app-server", "proxy"], ["app-server", "--listen=ws://127.0.0.1:1234"],
                     ["app-server", "--listen"], ["app-server", "--print-startup-config"],
                     ["exec", "app-server"], ["-m", "app-server"]):
            with self.subTest(argv=argv), self.assertRaises(services.ServiceError):
                codex_rpc.validate_arguments(argv)

    def test_proxy_forwards_other_messages_and_never_sends_model_list_to_child(self):
        from helpers import SRC
        child = ("import json,sys\n"
                 "for line in sys.stdin.buffer:\n"
                 " request=json.loads(line)\n"
                 " result={'id':request['id'],'result':{'echo':request['method'],"
                 "'size':len(request.get('payload',''))}}\n"
                 " sys.stdout.buffer.write((json.dumps(result)+'\\n').encode());sys.stdout.buffer.flush()\n")
        proxy = ("import os,sys\nfrom copilot_gateway.clients.codex_rpc import run\n"
                 f"raise SystemExit(run([sys.executable,'-u','-c',{child!r}],dict(os.environ),"
                 "[{'id':'one','displayName':'모델'},{'id':'two'}]))\n")
        env = {**os.environ, "PYTHONPATH": str(SRC) + os.pathsep + os.environ.get("PYTHONPATH", ""),
               "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"}
        requests = [{"id": 1, "method": "initialize"}, {"id": 2, "method": "model/list"},
                    {"id": 3, "method": "thread/start", "payload": "x" * 1_000_000},
                    {"id": 4, "method": "model/list"}]
        data = "\n".join(json.dumps(value) for value in requests).encode()
        result = subprocess.run([sys.executable, "-c", proxy], input=data, capture_output=True, env=env, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
        replies = {value["id"]: value for value in map(json.loads, result.stdout.splitlines())}
        self.assertEqual(replies[1]["result"]["echo"], "initialize")
        self.assertEqual(replies[2]["result"]["data"][0]["displayName"], "모델")
        self.assertEqual(replies[3]["result"]["echo"], "thread/start")
        self.assertEqual(replies[3]["result"]["size"], 1_000_000)
        self.assertEqual(replies[4]["result"]["data"][0]["id"], "one")


class PickerClientTests(unittest.TestCase):
    def test_version_checks_fail_closed(self):
        ok = mock.Mock(returncode=0, stdout="codex-cli 0.167.0")
        with mock.patch.object(model_picker.subprocess, "run", return_value=ok):
            model_picker.require_version("codex", "Codex", model_picker.CODEX_MIN_VERSION)
        for result in (mock.Mock(returncode=0, stdout="codex-cli 0.166.9"),
                       mock.Mock(returncode=1, stdout="codex-cli 0.167.0"),
                       mock.Mock(returncode=0, stdout="unrecognized")):
            with self.subTest(stdout=result.stdout), \
                    mock.patch.object(model_picker.subprocess, "run", return_value=result), \
                    self.assertRaises(services.ServiceError):
                model_picker.require_version("codex", "Codex", model_picker.CODEX_MIN_VERSION)

    def test_bundled_metadata_uses_the_installed_client_offline(self):
        result = mock.Mock(returncode=0, stdout=b'{"models":[]}')
        with mock.patch.object(model_picker.subprocess, "run", return_value=result) as run:
            self.assertEqual(model_picker._read_bundled_codex("/test/codex", {"CODEX_HOME": "isolated"}),
                             {"models": []})
        self.assertEqual(run.call_args.args[0], ["/test/codex", "debug", "models", "--bundled"])
        self.assertEqual(run.call_args.kwargs["env"], {"CODEX_HOME": "isolated"})


if __name__ == "__main__":
    unittest.main()
