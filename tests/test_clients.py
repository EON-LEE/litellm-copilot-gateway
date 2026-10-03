"""ccp / ccx launcher logic (no real claude/codex processes)."""
from pathlib import Path
import tempfile
import tomllib
import unittest

from helpers import TempHome, catalog

from copilot_gateway import gateway, services
from copilot_gateway.clients import claude, codex


class ClaudeLauncherTests(unittest.TestCase):
    def setUp(self):
        self.home = TempHome()
        self.addCleanup(self.home.cleanup)
        self.settings = self.home.settings()

    def test_default_model_is_forced_unless_user_chose_one(self):
        self.assertEqual(claude.claude_args([], "claude-sonnet-5"),
                         ["--dangerously-skip-permissions", "--model", "claude-sonnet-5"])
        for argv in (["--model", "claude-opus-5"], ["--model=claude-opus-5"], ["-p", "hi", "--model", "x"]):
            with self.subTest(argv=argv):
                self.assertEqual(claude.claude_args(argv, "claude-sonnet-5"),
                                 ["--dangerously-skip-permissions", *argv])
        self.assertEqual(claude.claude_args(["--", "--model", "x"], "claude-sonnet-5"),
                         ["--dangerously-skip-permissions", "--model", "claude-sonnet-5", "--", "--model", "x"])

    def test_env_points_at_gateway_and_keeps_user_overrides(self):
        env = claude.claude_env(self.settings, "k", base={"ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-gpt-6-astra",
                                                         "ANTHROPIC_BASE_URL": "https://api.anthropic.com"})
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "http://127.0.0.1:24000")
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "k")
        self.assertEqual(env["CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"], "1")
        self.assertEqual(env["ANTHROPIC_DEFAULT_OPUS_MODEL"], "claude-gpt-6-astra")
        self.assertEqual(env["ANTHROPIC_DEFAULT_HAIKU_MODEL"], "claude-haiku-4.5")
        self.assertEqual(env["ANTHROPIC_SMALL_FAST_MODEL"], "gpt-5-mini")

    def test_default_models_exist_in_the_generated_config(self):
        gateway.refresh(self.settings, catalog())
        from copilot_gateway import catalog as cat
        config = cat.parse_rendered(self.settings.claude_config.read_text(encoding="utf-8"))
        names = {row["model_name"] for row in config["model_list"]}
        names |= set(config["router_settings"]["model_group_alias"])
        for value in claude.DEFAULTS.values():
            self.assertIn(value, names)


class CodexLauncherTests(unittest.TestCase):
    def setUp(self):
        self.home = TempHome()
        self.addCleanup(self.home.cleanup)
        self.settings = self.home.settings()

    def test_fresh_config_selects_gateway_provider(self):
        parsed = tomllib.loads(codex.render_codex_config("", 24001, "gpt-5.5"))
        self.assertEqual(parsed["model_provider"], codex.PROVIDER)
        self.assertEqual(parsed["model"], "gpt-5.5")
        provider = parsed["model_providers"][codex.PROVIDER]
        self.assertEqual(provider["base_url"], "http://127.0.0.1:24001/v1")
        self.assertEqual((provider["wire_api"], provider["env_key"]), ("responses", "LITELLM_MASTER_KEY"))

    def test_user_edits_survive_and_rewrite_is_idempotent(self):
        first = codex.render_codex_config("", 24001, "gpt-5.5")
        edited = first + '\nmodel_reasoning_effort = "high"\n[projects."C:/work"]\ntrust_level = "trusted"\n'
        edited = edited.replace('model = "gpt-5.5"', 'model = "claude-opus-5"')
        again = codex.render_codex_config(edited, 24009, "gpt-5.5")
        parsed = tomllib.loads(again)
        self.assertEqual(parsed["model"], "claude-opus-5")
        self.assertEqual(parsed["model_reasoning_effort"], "high")
        self.assertEqual(parsed["projects"]["C:/work"]["trust_level"], "trusted")
        self.assertEqual(parsed["model_providers"][codex.PROVIDER]["base_url"], "http://127.0.0.1:24009/v1")
        self.assertEqual(codex.render_codex_config(again, 24009, "gpt-5.5"), again)

    def test_foreign_provider_or_invalid_toml_is_rejected(self):
        for text in ('model_provider = "openai"\n', '[model_providers.copilot_gateway]\nname = "x"\n', "= broken"):
            with self.subTest(text=text), self.assertRaises(services.ServiceError):
                codex.render_codex_config(text, 24001, "gpt-5.5")

    def test_write_codex_home_is_private_to_ccgw(self):
        home = codex.write_codex_home(self.settings, "gpt-5.5")
        self.assertTrue(str(home).startswith(str(self.home.path)))
        self.assertTrue((home / "config.toml").exists())
        env = codex.codex_env(self.settings, "k", base={})
        self.assertEqual(env, {"CODEX_HOME": str(home), "LITELLM_MASTER_KEY": "k"})

    def test_default_model_prefers_env_then_known_gpt(self):
        value = catalog()
        gateway.refresh(self.settings, value)
        self.assertEqual(codex.default_model(self.settings, {"CCX_MODEL": "claude-opus-5"}), "claude-opus-5")
        self.assertEqual(codex.default_model(self.settings, {}), "gpt-5-mini")

    def test_find_codex_honours_override(self):
        self.assertEqual(codex.find_codex({"CCX_CODEX": "/opt/codex"}), "/opt/codex")

    def test_winget_codex_without_alias(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / "Microsoft" / "WinGet" / "Packages" / "OpenAI.Codex_Microsoft.Winget.Source_x"
            package.mkdir(parents=True)
            for name in ("codex-command-runner.exe", "codex-x86_64-pc-windows-msvc.exe"):
                (package / name).write_bytes(b"")
            (package / "codex-aarch64-pc-windows-msvc.exe").mkdir()
            with mock.patch.object(codex.sys, "platform", "win32"):
                self.assertEqual(codex.winget_codex({"LOCALAPPDATA": tmp}),
                                 str(package / "codex-x86_64-pc-windows-msvc.exe"))
                self.assertIsNone(codex.winget_codex({"LOCALAPPDATA": str(Path(tmp) / "none")}))
            with mock.patch.object(codex.sys, "platform", "linux"):
                self.assertIsNone(codex.winget_codex({"LOCALAPPDATA": tmp}))


class HelpTests(unittest.TestCase):
    def test_help_never_starts_or_installs_anything(self):
        from unittest import mock
        boom = mock.Mock(side_effect=AssertionError("help must not touch the gateway"))
        with mock.patch.object(gateway, "ensure", boom), mock.patch("copilot_gateway.settings.Settings.load", boom), \
                mock.patch.object(codex, "find_codex", return_value=None), \
                mock.patch("shutil.which", return_value=None), mock.patch.dict("os.environ", {}, clear=False):
            for argv in (["--help"], ["-h"], ["--version"], ["help"]):
                with self.subTest(argv=argv):
                    self.assertEqual(codex.main(argv), 0)
                    self.assertEqual(claude.main(argv), 0)

    def test_missing_executable_is_reported_not_raised(self):
        self.assertEqual(claude.run(["definitely-not-a-real-binary-ccgw"], {}), 127)


if __name__ == "__main__":
    unittest.main()
