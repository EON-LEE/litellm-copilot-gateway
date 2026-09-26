"""Exercise refresh-models.sh with a local stand-in for the two remote GETs."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[1]


def model(model_id, name, context, prompt, output, endpoints, **extra):
    return {
        "id": model_id, "name": name, "vendor": "Anthropic" if model_id.startswith("claude-") else "OpenAI",
        "model_picker_enabled": True, "preview": False,
        "policy": {"state": "enabled"}, "supported_endpoints": endpoints,
        "capabilities": {"type": "chat", "limits": {
            "max_context_window_tokens": context, "max_prompt_tokens": prompt,
            "max_output_tokens": output,
        }}, **extra,
    }


def catalog():
    native = ["/v1/messages", "/chat/completions"]
    responses = ["/responses", "ws:/responses"]
    rows = [
        model("claude-opus-5", "Claude Opus 5", 1000000, 936000, 64000, native),
        model("claude-sonnet-5", "Claude Sonnet 5", 1000000, 936000, 64000, native),
        model("claude-haiku-4.5", "Claude Haiku 4.5", 200000, 136000, 64000, native),
        model("gpt-6-astra", "GPT-6 Astra", 1000000, 872000, 128000, responses),
        model("gpt-5-mini", "GPT-5 mini", 264000, 128000, 64000, responses),
        model("gemini-3.8-flash", "Gemini 3.8 Flash", 1048576, 983040, 65536, ["/chat/completions"], policy=None),
        model("gpt-4.1", "GPT-4.1", 128000, 128000, 16000, [], model_picker_enabled=False),
        model("gpt-4o-2024-05-13", "GPT-4o snapshot", 128000, 64000, 16000, [], model_picker_enabled=False),
        model("gpt-5.6-sol-fast", "GPT-5.6 Sol Fast (Internal only)", 1000000, 922000, 78000, responses),
        model("gpt-disabled", "Unavailable GPT", 1000000, 922000, 78000, responses, policy={"state": "disabled"}),
        model("trajectory-compaction", "Internal compaction", 256000, 245760, 10240, ["/chat/completions"], model_picker_enabled=False),
    ]
    for row in rows[:3]:
        row["capabilities"]["limits"]["max_non_streaming_output_tokens"] = 16000
    rows.append({"id": "text-embedding-3-small", "model_picker_enabled": True, "capabilities": {"type": "embeddings"}})
    return {"object": "list", "data": rows}


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.repo = self.home / "litellm-copilot-gateway"
        self.repo.mkdir()
        for name in ("refresh-models.sh", "list-models.sh", "model_catalog.py"):
            source = ROOT / name
            if source.exists():
                shutil.copy2(source, self.repo / name)
        credential = self.home / ".config/litellm/github_copilot"
        credential.mkdir(parents=True)
        (credential / "access-token").write_text("test-oauth-not-a-secret")
        self.input = self.home / "catalog.json"
        self.input.write_text(json.dumps(catalog()))
        self.bin = self.home / "bin"
        self.bin.mkdir()
        curl = self.bin / "curl"
        curl.write_text(f'''#!{sys.executable}
import os, sys
from pathlib import Path
args = sys.argv[1:]
if "https://api.github.com/copilot_internal/v2/token" in args:
    print('{{"token":"test-bearer-not-a-secret"}}')
elif "https://api.githubcopilot.com/models" in args:
    print(Path(os.environ["TEST_CATALOG"]).read_text())
else:
    raise SystemExit("unexpected URL in refresh")
''')
        curl.chmod(0o755)
        self.env = {**os.environ, "HOME": str(self.home), "TEST_CATALOG": str(self.input),
                    "PATH": f"{self.bin}:{os.environ['PATH']}", "LITELLM_LOCAL_MODEL_COST_MAP": "True"}

    def refresh(self, value=None):
        if value is not None:
            self.input.write_text(json.dumps(value))
        return subprocess.run(["bash", str(self.repo / "refresh-models.sh")],
                              env=self.env, text=True, capture_output=True, timeout=15)

    def config(self, value=None):
        result = self.refresh(value)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return yaml.safe_load((self.repo / "config.yaml").read_text())

    def test_one_public_name_per_user_visible_copilot_id(self):
        cfg = self.config()
        expected = {
            "claude-opus-5": "anthropic/claude-opus-5",
            "claude-sonnet-5": "anthropic/claude-sonnet-5",
            "claude-haiku-4.5": "anthropic/claude-haiku-4.5",
            "claude-gpt-6-astra": "anthropic/gpt-6-astra",
            "claude-gpt-5-mini": "anthropic/gpt-5-mini",
            "claude-gemini-3.8-flash": "github_copilot/gemini-3.8-flash",
        }
        rows = cfg["model_list"]
        self.assertEqual({r["model_name"]: r["litellm_params"]["model"] for r in rows}, expected)
        self.assertEqual(len(rows), len(expected))
        self.assertEqual(len({r["litellm_params"]["model"] for r in rows}), len(rows))

    def test_metadata_uses_catalog_name_and_independent_token_limits(self):
        cfg = self.config()
        rows = {r["model_name"]: r for r in cfg["model_list"]}
        mini = rows["claude-gpt-5-mini"].get("model_info", {})
        self.assertEqual(mini.get("display_name"), "GPT-5 mini")
        self.assertEqual(mini.get("gateway_provider"), "github_copilot")
        self.assertEqual(mini.get("upstream_model_id"), "gpt-5-mini")
        self.assertEqual(mini.get("max_context_window_tokens"), 264000)
        self.assertEqual(mini.get("max_input_tokens"), 128000)
        self.assertEqual(mini.get("max_output_tokens"), 64000)
        self.assertIn("264K", mini.get("description", ""))
        self.assertIn("128K", mini.get("description", ""))
        haiku = rows["claude-haiku-4.5"].get("model_info", {})
        self.assertEqual(haiku.get("max_non_streaming_output_tokens"), 16000)
        self.assertIn("16K", haiku.get("description", ""))
        self.assertLessEqual(len(haiku["description"]), 100)

    def test_only_same_model_compatibility_spellings_are_hidden(self):
        cfg = self.config()
        aliases = cfg.get("router_settings", {}).get("model_group_alias", {})
        self.assertEqual(aliases.get("gpt-6-astra"), {"model": "claude-gpt-6-astra", "hidden": True})
        self.assertEqual(aliases.get("claude-haiku-4-5"), {"model": "claude-haiku-4.5", "hidden": True})
        self.assertEqual(aliases.get("claude-gpt-6-astra[1m]"), {"model": "claude-gpt-6-astra", "hidden": True})
        self.assertEqual(aliases.get("gpt-6-astra[1m]"), {"model": "claude-gpt-6-astra", "hidden": True})
        for retired in ("claude-fable-5", "claude-mythos-5", "claude-opus-4.6", "claude-sonnet-4.6",
                        "claude-3-5-haiku-20241022", "claude-haiku-4.5[1m]", "claude-haiku-4-5[1m]",
                        "gpt-5-mini[1m]", "claude-gpt-5-mini[1m]"):
            self.assertNotIn(retired, aliases)
        self.assertTrue(all(alias["hidden"] is True for alias in aliases.values()))

    def test_public_router_list_has_no_wildcard_expansion_or_alias_rows(self):
        from litellm import Router
        from litellm.proxy.auth.model_checks import get_complete_model_list
        from litellm.llms.github_copilot.authenticator import Authenticator
        self.enterContext(mock.patch.dict(os.environ, {
            "GITHUB_COPILOT_TOKEN_DIR": str(self.home / "router-test-tokens"),
        }))
        for method, value in (("get_api_key", "offline-test-not-a-secret"),
                              ("get_api_base", "https://api.githubcopilot.com")):
            self.enterContext(mock.patch.object(Authenticator, method, return_value=value))
        for target in ("socket.socket.connect", "socket.socket.connect_ex", "socket.create_connection"):
            self.enterContext(mock.patch(target, side_effect=AssertionError("Network forbidden in catalog tests")))
        cfg = self.config()
        router = Router(model_list=cfg["model_list"], **cfg.get("router_settings", {}))
        names = get_complete_model_list(key_models=[], team_models=[], proxy_model_list=router.get_model_names(),
                                        user_model=None, infer_model_from_keys=False, llm_router=router)
        self.assertEqual(set(names), {"claude-opus-5", "claude-sonnet-5", "claude-haiku-4.5",
                                      "claude-gpt-6-astra", "claude-gpt-5-mini", "claude-gemini-3.8-flash"})
        self.assertEqual(len(names), 6)
        for name in ("claude-haiku-4.5", "claude-haiku-4-5"):
            deployment = router.get_available_deployment(model=name, messages=[])
            self.assertEqual(deployment["litellm_params"]["model"], "anthropic/claude-haiku-4.5")
        public = {row["model_name"]: row for row in cfg["model_list"]}
        for alias, settings in cfg["router_settings"]["model_group_alias"].items():
            with self.subTest(alias=alias):
                deployment = router.get_available_deployment(model=alias, messages=[])
                self.assertEqual(deployment["litellm_params"]["model"],
                                 public[settings["model"]]["litellm_params"]["model"])
        for name in ("claude-fable-5", "gpt-4.1", "gpt-5.6-sol-fast", "gpt-disabled", "totally-unknown"):
            with self.subTest(name=name), self.assertRaises(Exception):
                router.get_available_deployment(model=name, messages=[])

    def test_new_picker_model_is_added_without_a_hardcoded_allowlist(self):
        value = catalog()
        value["data"].append(model("gpt-future", "Future GPT", 2000000, 1800000, 200000, ["/responses"]))
        cfg = self.config(value)
        names = [row["model_name"] for row in cfg["model_list"]]
        self.assertEqual(names.count("claude-gpt-future"), 1)
        self.assertNotIn("gpt-future", names)
        self.assertNotIn("claude-gpt-future[1m]", names)

    def test_legacy_small_fast_model_keeps_hosted_websearch_route_if_rediscovered(self):
        value = catalog()
        value["data"].append(model("gpt-4o-mini", "GPT-4o mini", 128000, 128000, 16000,
                                   ["/chat/completions"]))
        cfg = self.config(value)
        rows = [row for row in cfg["model_list"] if row["model_info"]["upstream_model_id"] == "gpt-4o-mini"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["litellm_params"], {
            "model": "anthropic/gpt-4o-mini", "api_base": "http://localhost:4141", "api_key": "dummy",
        })
        self.assertEqual(cfg["router_settings"]["model_group_alias"]["gpt-4o-mini"],
                         {"model": "claude-gpt-4o-mini", "hidden": True})

    def test_canonical_and_compatibility_alias_collisions_refuse_refresh(self):
        self.config()
        before = (self.repo / "config.yaml").read_bytes()
        for model_id in ("claude-gpt-6-astra", "claude-haiku-4-5"):
            value = catalog()
            value["data"].append(model(model_id, "Conflicting model", 200000, 136000, 64000,
                                       ["/v1/messages"]))
            with self.subTest(model_id=model_id):
                result = self.refresh(value)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual((self.repo / "config.yaml").read_bytes(), before)

    def test_new_metadata_and_1m_alias_threshold_are_not_hardcoded(self):
        value = catalog()
        value["data"].append(model("claude-opus-5.5", "Claude Opus 5.5", 1000000, 1000000, 128000,
                                   ["/v1/messages"]))
        for context in (999999, 1000000, 1050000):
            value["data"][3]["capabilities"]["limits"].update(
                max_context_window_tokens=context, max_prompt_tokens=context,
            )
            cfg = self.config(value)
            aliases = cfg["router_settings"]["model_group_alias"]
            self.assertEqual("gpt-6-astra[1m]" in aliases, context >= 1000000)
            self.assertEqual("claude-gpt-6-astra[1m]" in aliases, context >= 1000000)
            opus = next(row for row in cfg["model_list"] if row["model_name"] == "claude-opus-5.5")
            self.assertEqual(opus["model_info"]["max_input_tokens"], 1000000)
            self.assertEqual(opus["model_info"]["max_output_tokens"], 128000)
            self.assertEqual(aliases["claude-opus-5-5"]["model"], "claude-opus-5.5")

    def test_list_command_uses_same_selection_and_reports_actual_names_and_limits(self):
        result = subprocess.run(["bash", str(self.repo / "list-models.sh")], env=self.env,
                                text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stdout.strip().splitlines()), 7)
        self.assertIn("GPT-6 Astra", result.stdout)
        self.assertIn("872K", result.stdout)
        self.assertIn("264K", result.stdout)
        self.assertIn("16K", result.stdout)
        for name in ("gpt-4.1", "gpt-disabled", "Internal only", "[1m]", "claude-gpt-"):
            self.assertNotIn(name, result.stdout)

    def test_shuffled_unchanged_catalog_does_not_replace_config_or_backup(self):
        self.config()
        cfg = self.repo / "config.yaml"
        before = cfg.stat().st_ino, cfg.stat().st_mtime_ns, cfg.read_bytes()
        value = catalog()
        value["data"].reverse()
        self.config(value)
        self.assertEqual((cfg.stat().st_ino, cfg.stat().st_mtime_ns, cfg.read_bytes()), before)
        self.assertFalse((self.repo / "config.yaml.bak").exists())

    def test_missing_required_limits_do_not_fall_back_to_provider_cost_map(self):
        self.config()
        before = (self.repo / "config.yaml").read_bytes()
        for key in ("max_context_window_tokens", "max_prompt_tokens", "max_output_tokens"):
            for missing in (True, False):
                value = catalog()
                limits = value["data"][0]["capabilities"]["limits"]
                if missing:
                    del limits[key]
                else:
                    limits[key] = None
                with self.subTest(key=key, missing=missing):
                    result = self.refresh(value)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual((self.repo / "config.yaml").read_bytes(), before)

    def test_falsy_malformed_policy_or_limits_do_not_replace_config(self):
        self.config()
        before = (self.repo / "config.yaml").read_bytes()
        for key in ("policy", "limits"):
            for malformed in (False, [], "", 0):
                value = catalog()
                target = value["data"][0] if key == "policy" else value["data"][0]["capabilities"]
                target[key] = malformed
                with self.subTest(key=key, malformed=malformed):
                    result = self.refresh(value)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual((self.repo / "config.yaml").read_bytes(), before)

    def test_bad_catalog_does_not_overwrite_a_working_config(self):
        self.config()
        before = (self.repo / "config.yaml").read_bytes()
        values = [{"data": []}, {"data": None}]
        no_picker = catalog()
        for row in no_picker["data"]:
            row["model_picker_enabled"] = False
        values.append(no_picker)
        duplicate = catalog()
        duplicate["data"].append(copy.deepcopy(duplicate["data"][0]))
        values.append(duplicate)
        malformed = catalog()
        malformed["data"][0]["capabilities"]["limits"]["max_prompt_tokens"] = "unknown"
        values.append(malformed)
        for value in values:
            with self.subTest(value=value):
                result = self.refresh(value)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual((self.repo / "config.yaml").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
