"""copilot-api model-identity guard (synthetic bundles; the real pinned bundle when installed)."""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from copilot_gateway import copilot_api as capi
from copilot_gateway.settings import data_dir


def bundle(branch=capi.BRANCH):
    return "// head\n" + capi.PREFIX + branch + capi.SUFFIX + "// tail\n"


class IdentityPatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="capi-test-")
        self.addCleanup(self.temp.cleanup)
        self.server = Path(self.temp.name) / "server.js"

    def write(self, text):
        self.server.write_text(text, encoding="utf-8", newline="")

    def test_both_known_upstream_shapes_patch_once(self):
        for branch in (capi.BRANCH, capi.LEGACY_BRANCH):
            with self.subTest(legacy=branch is capi.LEGACY_BRANCH):
                self.write(bundle(branch))
                self.assertTrue(capi.patch_identity(self.server))
                patched = self.server.read_text(encoding="utf-8")
                self.assertIn(capi.MARKER, patched)
                self.assertNotIn("getSmallModel()", patched)
                self.assertFalse(capi.patch_identity(self.server))
                self.assertEqual(self.server.read_text(encoding="utf-8"), patched)

    def test_unknown_or_ambiguous_bundles_fail_without_writing(self):
        changed = bundle().replace("compactType === 0", "compactType == 0")
        for text in (changed, bundle() + capi.PREFIX, "// nothing\n",
                     bundle() + "anthropicPayload.model = getSmallModel();\n"):
            with self.subTest(text=text[-60:]):
                self.write(text)
                with self.assertRaises(capi.CopilotApiError):
                    capi.patch_identity(self.server)
                self.assertEqual(self.server.read_text(encoding="utf-8"), text)

    def test_cross_model_config_is_refused_without_changes(self):
        path = Path(self.temp.name) / "config.json"
        capi.check_identity_options(path)  # missing is fine
        for good in ("", "{}", '{"modelMappings": {}, "claudeAutoModel": ""}'):
            path.write_text(good, encoding="utf-8")
            capi.check_identity_options(path)
        for bad in ('{"modelMappings": {"a": "b"}}', '{"claudeAutoModel": "x"}', "[]", "{", '{"modelMappings": 1}'):
            with self.subTest(bad=bad):
                path.write_text(bad, encoding="utf-8")
                with self.assertRaises(capi.CopilotApiError):
                    capi.check_identity_options(path)
                self.assertEqual(path.read_text(encoding="utf-8"), bad)

    def test_http_transport_is_pinned_without_touching_other_keys(self):
        path = Path(self.temp.name) / "sub" / "config.json"
        self.assertTrue(capi.enforce_http_transport(path))
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"useResponsesApiWebSocket": False})
        path.write_text('{"useResponsesApiWebSocket": true, "smallModels": {"codex": "x"}}', encoding="utf-8")
        self.assertTrue(capi.enforce_http_transport(path))
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")),
                         {"useResponsesApiWebSocket": False, "smallModels": {"codex": "x"}})
        before = path.read_text(encoding="utf-8")
        self.assertFalse(capi.enforce_http_transport(path))
        self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_http_transport_fails_closed_on_unknown_upstream(self):
        source = Path(self.temp.name) / "config.js"
        path = Path(self.temp.name) / "config.json"
        source.write_text("return getConfig().somethingElse ?? true;", encoding="utf-8")
        with self.assertRaises(capi.CopilotApiError):
            capi.enforce_http_transport(path, source)
        self.assertFalse(path.exists())


class WebSearchModelTests(unittest.TestCase):
    OFFERED = ["gpt-6-luna", "gpt-6.1-sol"]

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="capi-ws-")
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name) / "config.js"
        self.source.write_text('\tconst model = getConfig().messageApiWebSearchModel ?? "gpt-6-luna";\n',
                               encoding="utf-8")
        self.path = Path(self.temp.name) / "config.json"

    def write(self, value):
        self.path.write_text(json.dumps(value), encoding="utf-8")
        return self.path.read_text(encoding="utf-8")

    def sync(self, available=None, override=None):
        return capi.sync_web_search_model(self.path, self.source, self.OFFERED if available is None else available,
                                          override)

    def test_retired_model_moves_to_upstream_default_and_keeps_other_keys(self):
        self.write({"messageApiWebSearchModel": "gpt-5-mini", "useResponsesApiWebSocket": False,
                    "extraPrompts": {"gpt-5-mini": "x"}})
        self.assertEqual(self.sync(), ("gpt-5-mini", "gpt-6-luna"))
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")),
                         {"messageApiWebSearchModel": "gpt-6-luna", "useResponsesApiWebSocket": False,
                          "extraPrompts": {"gpt-5-mini": "x"}})
        self.assertIsNone(self.sync())

    def test_offered_disabled_and_provider_values_are_kept(self):
        for value in ("gpt-6.1-sol", "", "openrouter/some-model"):
            with self.subTest(value=value):
                before = self.write({"messageApiWebSearchModel": value})
                self.assertIsNone(self.sync())
                self.assertEqual(self.path.read_text(encoding="utf-8"), before)

    def test_missing_config_relies_on_upstream_default_without_writing(self):
        self.assertIsNone(self.sync())
        self.assertEqual(self.sync(["gpt-6.1-sol"]), ("gpt-6-luna", None))
        self.assertFalse(self.path.exists())

    def test_unreplaceable_retired_model_is_reported_not_changed(self):
        before = self.write({"messageApiWebSearchModel": "gpt-5-mini"})
        self.assertEqual(self.sync(["gpt-6.1-sol"]), ("gpt-5-mini", None))
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)

    def test_override_must_be_an_offered_model(self):
        self.write({"messageApiWebSearchModel": "gpt-6-luna"})
        self.assertEqual(self.sync(override="gpt-6.1-sol"), ("gpt-6-luna", "gpt-6.1-sol"))
        self.assertIsNone(self.sync(override="gpt-6.1-sol"))
        with self.assertRaises(capi.CopilotApiError):
            self.sync(override="gpt-5-mini")

    def test_bad_values_and_unknown_upstream_fail_closed(self):
        for config in ({"messageApiWebSearchModel": 1}, []):
            with self.subTest(config=config):
                before = self.write(config)
                with self.assertRaises(capi.CopilotApiError):
                    self.sync()
                self.assertEqual(self.path.read_text(encoding="utf-8"), before)
        before = self.write({"messageApiWebSearchModel": "gpt-5-mini"})
        self.source.write_text("return getConfig().webSearchModel;", encoding="utf-8")
        with self.assertRaises(capi.CopilotApiError):
            self.sync()
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)


class GatewayWebSearchTests(unittest.TestCase):
    def setUp(self):
        from helpers import TempHome, catalog
        self.home = TempHome()
        self.addCleanup(self.home.cleanup)
        self.settings = self.home.settings()
        self.catalog = catalog

    def test_sync_offers_only_openai_responses_models_and_honours_override(self):
        from copilot_gateway import gateway
        with mock.patch.object(gateway.copilot_api, "sync_installed_web_search") as sync:
            self.assertFalse(gateway.sync_web_search(self.settings, log=lambda *_: None, environ={}))
            sync.assert_not_called()
            gateway.refresh(self.settings, self.catalog())
            sync.return_value = ("gpt-5-mini", "gpt-6-astra")
            messages = []
            self.assertTrue(gateway.sync_web_search(self.settings, log=messages.append, environ={}))
            sync.assert_called_with(self.settings.home, ["gpt-5-mini", "gpt-6-astra"], None, {})
            self.assertIn("gpt-5-mini -> gpt-6-astra", messages[0])
            (self.home.path / ".env").write_text("CCGW_WEBSEARCH_MODEL=gpt-6-astra\n", encoding="utf-8")
            settings = self.home.settings()
            gateway.sync_web_search(settings, log=messages.append, environ={})
            self.assertEqual(sync.call_args.args[2], "gpt-6-astra")
            gateway.sync_web_search(settings, log=messages.append, environ={"CCGW_WEBSEARCH_MODEL": "gpt-5-mini"})
            self.assertEqual(sync.call_args.args[2], "gpt-5-mini")
            sync.return_value = ("gpt-5-mini", None)
            self.assertFalse(gateway.sync_web_search(settings, log=messages.append, environ={}))
            self.assertIn("CCGW_WEBSEARCH_MODEL", messages[-1])

    def test_running_capi_restarts_only_after_a_change(self):
        from copilot_gateway import gateway, services
        running = services.State("running", [])
        capi = services.Service("capi", 24141, self.home.path / "capi.log", "capi")
        gateway.credential_file(self.settings, "capi").parent.mkdir(parents=True, exist_ok=True)
        gateway.credential_file(self.settings, "capi").write_text("fp\n", encoding="utf-8")
        for changed in (True, False):
            with self.subTest(changed=changed), \
                    mock.patch.object(gateway, "sync_web_search", return_value=changed), \
                    mock.patch.object(gateway.services, "inspect", return_value=running), \
                    mock.patch.object(gateway.services, "stop") as stop, \
                    mock.patch.object(gateway.services, "start", return_value=False) as start:
                gateway.start_capi(self.settings, {"capi": capi}, "fp", log=lambda *_: None)
                self.assertEqual(stop.call_count, int(changed))
                start.assert_called_once_with(capi, log=mock.ANY)


class TokenEnvTests(unittest.TestCase):
    def test_gateway_login_goes_to_copilot_api_except_in_its_own_auth_modes(self):
        self.assertEqual(capi.token_env("tok", {}), {capi.TOKEN_ENV: "tok"})
        for mode in ("COPILOT_API_OAUTH_APP", "COPILOT_API_ENTERPRISE_URL"):
            self.assertEqual(capi.token_env("tok", {mode: "x"}), {})

    def test_token_precedence_anchor_fails_closed(self):
        good = ('const GITHUB_TOKEN_ENV = "COPILOT_API_GITHUB_TOKEN";\n'
                'function readGitHubTokenFromEnv() {\n\treturn process.env["COPILOT_API_GITHUB_TOKEN"]?.trim();\n}\n'
                "async function resolveGitHubToken(cliToken) {\n\tif (cliToken) return cliToken;\n"
                "\tconst envToken = readGitHubTokenFromEnv();\n\tif (envToken) return envToken;\n"
                "\tconst fileToken = await readGitHubToken();\n\treturn fileToken;\n}\n")
        swapped = good.replace("readGitHubTokenFromEnv();\n\tif (envToken) return envToken;\n"
                               "\tconst fileToken = await readGitHubToken();",
                               "readGitHubTokenFromEnv;\n\tconst fileToken = await readGitHubToken();\n"
                               "\tconst late = readGitHubTokenFromEnv();")
        for text, ok in ((good, True), (swapped, False), (good.replace('process.env["COPILOT_API_GITHUB_TOKEN"]', ""),
                                                          False), (good + good, False)):
            with self.subTest(ok=ok), tempfile.TemporaryDirectory() as temp:
                (Path(temp) / "dist").mkdir()
                (Path(temp) / "dist" / "token-x.js").write_text(text, encoding="utf-8")
                if ok:
                    capi.check_token_env(temp)
                else:
                    with self.assertRaises(capi.CopilotApiError):
                        capi.check_token_env(temp)


class NodeTests(unittest.TestCase):
    def test_version_is_parsed_and_checked(self):
        for stdout, expected in (("v22.13.0\n", (22, 13, 0)), ("v24.18.1", (24, 18, 1)), ("garbage", None)):
            with self.subTest(stdout=stdout), mock.patch.object(capi.subprocess, "run") as run:
                run.return_value.stdout = stdout
                self.assertEqual(capi.node_version("node"), expected)
        with mock.patch.object(capi.subprocess, "run", side_effect=OSError):
            self.assertIsNone(capi.node_version("node"))
        with mock.patch.object(capi.shutil, "which", return_value="/usr/bin/node"):
            for version in ((22, 12, 9), (20, 19, 0), None):
                with self.subTest(version=version), mock.patch.object(capi, "node_version", return_value=version), \
                        self.assertRaisesRegex(capi.CopilotApiError, "22.13"):
                    capi.node_executable()
            with mock.patch.object(capi, "node_version", return_value=(22, 13, 0)):
                self.assertEqual(capi.node_executable(), "/usr/bin/node")
        with mock.patch.object(capi.shutil, "which", return_value=None), \
                self.assertRaisesRegex(capi.CopilotApiError, "22.13"):
            capi.node_executable()


def installed_package():
    home = os.environ.get("CCGW_TEST_HOME") or str(data_dir())
    package = capi.package_dir(home)
    return package if (package / "package.json").exists() else None


@unittest.skipUnless(installed_package(), "pinned copilot-api not installed (run `ccgw setup`)")
class InstalledBundleTests(unittest.TestCase):
    def test_installed_bundle_is_pinned_and_patched(self):
        package = installed_package()
        self.assertEqual(json.loads((package / "package.json").read_text(encoding="utf-8"))["version"],
                         capi.EXPECTED_VERSION)
        with tempfile.TemporaryDirectory() as temp:
            copy = Path(temp) / "pkg"
            shutil.copytree(package / "dist", copy / "dist")
            shutil.copy(package / "package.json", copy / "package.json")
            _, server, config = capi.verify_package(copy)
            capi.patch_identity(server)
            self.assertIn(capi.MARKER, server.read_text(encoding="utf-8"))
            self.assertFalse(capi.patch_identity(server))
            self.assertTrue(capi.enforce_http_transport(Path(temp) / "config.json", config))
            self.assertRegex(capi.web_search_default(config), r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
            capi.check_token_env(copy)
        manifest = json.loads((package / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["engines"]["node"], ">=" + ".".join(map(str, capi.NODE_MINIMUM)))


if __name__ == "__main__":
    unittest.main()
