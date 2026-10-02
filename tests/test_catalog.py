"""Catalog → LiteLLM config generation and fail-closed refresh (offline)."""
import copy
import os
import unittest
from unittest import mock

from helpers import TempHome, catalog, model

from copilot_gateway import catalog as cat
from copilot_gateway import gateway

CAPI = "http://127.0.0.1:24141"


class ClaudeConfigTests(unittest.TestCase):
    def config(self, value=None):
        return cat.build_config(value or catalog(), CAPI)

    def test_one_public_name_per_user_visible_copilot_id(self):
        rows = self.config()["model_list"]
        self.assertEqual({r["model_name"]: r["litellm_params"]["model"] for r in rows}, {
            "claude-opus-5": "anthropic/claude-opus-5",
            "claude-sonnet-5": "anthropic/claude-sonnet-5",
            "claude-haiku-4.5": "anthropic/claude-haiku-4.5",
            "claude-gpt-6-astra": "anthropic/gpt-6-astra",
            "claude-gpt-5-mini": "anthropic/gpt-5-mini",
            "claude-gemini-3.8-flash": "github_copilot/gemini-3.8-flash",
        })
        for row in rows:
            if row["litellm_params"]["model"].startswith("anthropic/"):
                self.assertEqual(row["litellm_params"]["api_base"], CAPI)

    def test_metadata_uses_catalog_name_and_independent_token_limits(self):
        rows = {r["model_name"]: r["model_info"] for r in self.config()["model_list"]}
        mini = rows["claude-gpt-5-mini"]
        self.assertEqual((mini["display_name"], mini["gateway_provider"], mini["upstream_model_id"]),
                         ("GPT-5 mini", "github_copilot", "gpt-5-mini"))
        self.assertEqual((mini["max_context_window_tokens"], mini["max_input_tokens"], mini["max_output_tokens"]),
                         (264000, 128000, 64000))
        self.assertIn("264K", mini["description"])
        haiku = rows["claude-haiku-4.5"]
        self.assertEqual(haiku["max_non_streaming_output_tokens"], 16000)
        self.assertLessEqual(len(haiku["description"]), 100)

    def test_only_same_model_compatibility_spellings_are_hidden(self):
        aliases = self.config()["router_settings"]["model_group_alias"]
        self.assertEqual(aliases["gpt-6-astra"], {"model": "claude-gpt-6-astra", "hidden": True})
        self.assertEqual(aliases["claude-haiku-4-5"], {"model": "claude-haiku-4.5", "hidden": True})
        self.assertEqual(aliases["gpt-6-astra[1m]"], {"model": "claude-gpt-6-astra", "hidden": True})
        for retired in ("claude-fable-5", "claude-opus-4.6", "claude-haiku-4.5[1m]", "gpt-5-mini[1m]"):
            self.assertNotIn(retired, aliases)
        self.assertTrue(all(alias["hidden"] is True for alias in aliases.values()))

    def test_new_picker_model_is_added_without_a_hardcoded_allowlist(self):
        value = catalog()
        value["data"].append(model("gpt-future", "Future GPT", 2000000, 1800000, 200000, ["/responses"]))
        names = [row["model_name"] for row in self.config(value)["model_list"]]
        self.assertEqual(names.count("claude-gpt-future"), 1)

    def test_legacy_small_fast_model_keeps_hosted_websearch_route(self):
        value = catalog()
        value["data"].append(model("gpt-4o-mini", "GPT-4o mini", 128000, 128000, 16000, ["/chat/completions"]))
        row = next(r for r in self.config(value)["model_list"] if r["model_name"] == "claude-gpt-4o-mini")
        self.assertEqual(row["litellm_params"], {"model": "anthropic/gpt-4o-mini", "api_base": CAPI,
                                                 "api_key": "dummy"})

    def test_1m_alias_threshold_follows_catalog(self):
        value = catalog()
        for context in (999999, 1000000):
            value["data"][3]["capabilities"]["limits"].update(max_context_window_tokens=context,
                                                              max_prompt_tokens=context)
            aliases = self.config(value)["router_settings"]["model_group_alias"]
            self.assertEqual("gpt-6-astra[1m]" in aliases, context >= 1000000)

    def test_collisions_and_bad_catalogs_raise(self):
        bad = []
        for model_id in ("claude-gpt-6-astra", "claude-haiku-4-5"):
            value = catalog()
            value["data"].append(model(model_id, "Conflict", 200000, 136000, 64000, ["/v1/messages"]))
            bad.append(value)
        bad += [{"data": []}, {"data": None}]
        duplicate = catalog()
        duplicate["data"].append(copy.deepcopy(duplicate["data"][0]))
        bad.append(duplicate)
        for key in ("max_context_window_tokens", "max_prompt_tokens", "max_output_tokens"):
            value = catalog()
            del value["data"][0]["capabilities"]["limits"][key]
            bad.append(value)
        for malformed in (False, [], "", 0):
            value = catalog()
            value["data"][0]["policy"] = malformed
            bad.append(value)
        for value in bad:
            with self.subTest(value=str(value)[:80]), self.assertRaises(cat.CatalogError):
                self.config(value)

    def test_render_round_trips(self):
        config = self.config()
        text = cat.render(config)
        self.assertTrue(text.startswith("# AUTO-GENERATED"))
        self.assertEqual(cat.parse_rendered(text), config)


class CodexConfigTests(unittest.TestCase):
    def test_plain_ids_and_native_routes(self):
        config = cat.build_codex_config(catalog(), CAPI)
        routes = {r["model_name"]: r["litellm_params"] for r in config["model_list"]}
        self.assertEqual(set(routes), {"claude-opus-5", "claude-sonnet-5", "claude-haiku-4.5", "gpt-6-astra",
                                       "gpt-5-mini", "gemini-3.8-flash"})
        self.assertEqual(routes["claude-opus-5"], {"model": "anthropic/claude-opus-5", "api_base": CAPI,
                                                   "api_key": "dummy"})
        self.assertEqual(routes["gpt-6-astra"], {"model": "openai/gpt-6-astra", "api_base": CAPI + "/v1",
                                                 "api_key": "dummy"})
        self.assertEqual(routes["gemini-3.8-flash"]["model"], "github_copilot/gemini-3.8-flash")
        self.assertEqual(config["router_settings"]["model_group_alias"],
                         {"claude-haiku-4-5": {"model": "claude-haiku-4.5", "hidden": True}})


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.home = TempHome()
        self.addCleanup(self.home.cleanup)
        self.settings = self.home.settings()

    def snapshot(self):
        return {p: p.read_bytes() for p in (self.settings.claude_config, self.settings.codex_config)}

    def test_writes_both_configs_then_is_stable(self):
        self.assertEqual(gateway.refresh(self.settings, catalog()), {"claude": True, "codex": True})
        before = self.snapshot()
        shuffled = catalog()
        shuffled["data"].reverse()
        self.assertEqual(gateway.refresh(self.settings, shuffled), {"claude": False, "codex": False})
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.settings.claude_config.with_name("config.yaml.bak").exists())

    def test_bad_catalog_leaves_both_configs_untouched(self):
        gateway.refresh(self.settings, catalog())
        before = self.snapshot()
        with self.assertRaises(cat.CatalogError):
            gateway.refresh(self.settings, {"data": []})
        self.assertEqual(self.snapshot(), before)

    def test_change_keeps_a_backup(self):
        gateway.refresh(self.settings, catalog())
        value = catalog()
        value["data"].append(model("gpt-future", "Future GPT", 2000000, 1800000, 200000, ["/responses"]))
        self.assertEqual(gateway.refresh(self.settings, value), {"claude": True, "codex": True})
        self.assertTrue(self.settings.claude_config.with_name("config.yaml.bak").exists())

    def test_fetch_catalog_requires_login(self):
        with self.assertRaises(cat.CatalogError):
            cat.fetch_catalog(self.settings.token_dir)


class RouterTests(unittest.TestCase):
    def test_public_router_list_has_no_alias_rows_and_aliases_resolve_same_model(self):
        from litellm import Router
        from litellm.llms.github_copilot.authenticator import Authenticator
        from litellm.proxy.auth.model_checks import get_complete_model_list
        home = TempHome()
        self.addCleanup(home.cleanup)
        self.enterContext(mock.patch.dict(os.environ, {"GITHUB_COPILOT_TOKEN_DIR": str(home.path / "tok")}))
        for method, value in (("get_api_key", "offline-test"), ("get_api_base", "https://api.githubcopilot.com")):
            self.enterContext(mock.patch.object(Authenticator, method, return_value=value))
        # Windows asyncio needs loopback socketpair connect, so forbid name resolution instead.
        for target in ("socket.getaddrinfo", "socket.create_connection"):
            self.enterContext(mock.patch(target, side_effect=AssertionError("network forbidden")))
        for build in (cat.build_config, cat.build_codex_config):
            config = build(catalog(), CAPI)
            router = Router(model_list=config["model_list"], **config["router_settings"])
            names = get_complete_model_list(key_models=[], team_models=[], proxy_model_list=router.get_model_names(),
                                            user_model=None, infer_model_from_keys=False, llm_router=router)
            public = {row["model_name"]: row for row in config["model_list"]}
            self.assertEqual(set(names), set(public))
            for alias, target in config["router_settings"]["model_group_alias"].items():
                with self.subTest(alias=alias):
                    deployment = router.get_available_deployment(model=alias, messages=[])
                    self.assertEqual(deployment["litellm_params"]["model"],
                                     public[target["model"]]["litellm_params"]["model"])
            for name in ("claude-fable-5", "gpt-4.1", "gpt-5.6-sol-fast", "gpt-disabled", "totally-unknown"):
                with self.subTest(name=name), self.assertRaises(Exception):
                    router.get_available_deployment(model=name, messages=[])


if __name__ == "__main__":
    unittest.main()
