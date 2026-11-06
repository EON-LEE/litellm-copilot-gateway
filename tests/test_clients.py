"""ccp / ccx launcher logic (no real claude/codex processes)."""
from pathlib import Path
import tempfile
import tomllib
import unittest

from helpers import TempHome, catalog, model

from copilot_gateway import gateway, services
from copilot_gateway.clients import claude, codex


class ClaudeLauncherTests(unittest.TestCase):
    def setUp(self):
        self.home = TempHome()
        self.addCleanup(self.home.cleanup)
        self.settings = self.home.settings()
        gateway.refresh(self.settings, catalog())

    def config(self, value):
        from copilot_gateway import catalog as cat
        gateway.refresh(self.settings, value)
        return cat.parse_rendered(self.settings.claude_config.read_text(encoding="utf-8"))

    def test_default_model_is_forced_unless_user_chose_one(self):
        self.assertEqual(claude.claude_args([], "claude-sonnet-5"),
                         ["--dangerously-skip-permissions", "--model", "claude-sonnet-5"])
        for argv in (["--model", "claude-opus-5"], ["--model=claude-opus-5"], ["-p", "hi", "--model", "x"]):
            with self.subTest(argv=argv):
                self.assertEqual(claude.claude_args(argv, "claude-sonnet-5"),
                                 ["--dangerously-skip-permissions", *argv])
        self.assertEqual(claude.claude_args(["--", "--model", "x"], "claude-sonnet-5"),
                         ["--dangerously-skip-permissions", "--model", "claude-sonnet-5", "--", "--model", "x"])

    def test_caller_can_control_permissions_without_losing_model_or_arguments(self):
        argv = ["--permission-mode", "default", "--output-format", "stream-json"]
        self.assertEqual(claude.claude_args(argv, "claude-sonnet-5", skip_permissions=False),
                         ["--model", "claude-sonnet-5", *argv])
        for argv in (["--model", "claude-opus-5"], ["--dangerously-skip-permissions", "--model=x"]):
            with self.subTest(argv=argv):
                self.assertEqual(claude.claude_args(argv, "claude-sonnet-5", skip_permissions=False), argv)
        self.assertEqual(claude.claude_args(["--", "--model", "x"], "claude-sonnet-5", skip_permissions=False),
                         ["--model", "claude-sonnet-5", "--", "--model", "x"])

    def test_env_points_at_gateway_and_keeps_user_overrides(self):
        env = claude.claude_env(self.settings, "k", base={"ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-gpt-6-astra",
                                                         "ANTHROPIC_BASE_URL": "https://api.anthropic.com"})
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "http://127.0.0.1:24000")
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "k")
        self.assertEqual(env["CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"], "1")
        self.assertEqual(env["ANTHROPIC_DEFAULT_OPUS_MODEL"], "claude-gpt-6-astra")
        self.assertEqual(env["ANTHROPIC_MODEL"], "claude-sonnet-5")
        self.assertEqual(env["ANTHROPIC_DEFAULT_HAIKU_MODEL"], "claude-haiku-4.5")
        self.assertEqual(env["ANTHROPIC_SMALL_FAST_MODEL"], "claude-haiku-4.5")
        self.assertNotIn("ANTHROPIC_DEFAULT_FABLE_MODEL", env)

    def test_default_models_exist_in_the_generated_config(self):
        config = claude.load_config(self.settings)
        names = {row["model_name"] for row in config["model_list"]}
        for value in claude.model_defaults(config).values():
            self.assertIn(value, names)

    def test_newest_family_version_wins_and_missing_family_stays_unset(self):
        value = catalog()
        value["data"] = [row for row in value["data"] if row["id"] != "claude-opus-5"]
        value["data"] += [model("claude-sonnet-5.5", "Claude Sonnet 5.5", 1000000, 936000, 128000, ["/v1/messages"]),
                          model("claude-haiku-5.5", "Claude Haiku 5.5", 1000000, 936000, 128000, ["/v1/messages"]),
                          model("claude-opus-4.8-fast", "Claude Opus 4.8 fast", 1000000, 936000, 64000,
                                ["/v1/messages"])]
        env = claude.claude_env(self.settings, "k", base={}, config=self.config(value))
        self.assertEqual((env["ANTHROPIC_MODEL"], env["ANTHROPIC_DEFAULT_SONNET_MODEL"]),
                         ("claude-sonnet-5.5", "claude-sonnet-5.5"))
        self.assertEqual((env["ANTHROPIC_DEFAULT_HAIKU_MODEL"], env["ANTHROPIC_SMALL_FAST_MODEL"]),
                         ("claude-haiku-5.5", "claude-haiku-5.5"))
        self.assertNotIn("ANTHROPIC_DEFAULT_OPUS_MODEL", env)
        self.assertIn("opus: not offered", claude.describe(env))

    def test_main_model_falls_back_to_another_claude_family_only_when_sonnet_is_missing(self):
        value = catalog()
        value["data"] = [row for row in value["data"] if row["id"] != "claude-sonnet-5"]
        self.assertEqual(claude.model_defaults(self.config(value))["ANTHROPIC_MODEL"], "claude-opus-5")

    def test_unavailable_user_model_variables_are_reported(self):
        config = claude.load_config(self.settings)
        env = claude.claude_env(self.settings, "k", config=config, base={
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus-4.6", "ANTHROPIC_MODEL": "sonnet",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-haiku-4-5"})
        self.assertEqual(claude.unavailable_models(env, config), [("ANTHROPIC_DEFAULT_OPUS_MODEL", "claude-opus-4.6")])


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

    def test_default_model_prefers_env_then_most_capable_newest_openai(self):
        value = catalog()
        gateway.refresh(self.settings, value)
        self.assertEqual(codex.default_model(self.settings, {"CCX_MODEL": "claude-opus-5"}), "claude-opus-5")
        self.assertEqual(codex.default_model(self.settings, {}), "gpt-6-astra")
        value["data"] += [model("gpt-6.1-sol", "GPT-6.1 Sol", 1050000, 922000, 128000, ["/responses"],
                                model_picker_category="powerful"),
                          model("gpt-7-luna", "GPT-7 Luna", 1000000, 872000, 128000, ["/responses"],
                                model_picker_category="lightweight")]
        gateway.refresh(self.settings, value)
        self.assertEqual(codex.default_model(self.settings, {}), "gpt-6.1-sol")

    def test_retired_model_is_reported_but_never_rewritten(self):
        gateway.refresh(self.settings, catalog())
        home = codex.write_codex_home(self.settings, "gpt-5.5")
        before = (home / "config.toml").read_text(encoding="utf-8")
        warning = codex.model_warning(self.settings, [])
        self.assertIn("'gpt-5.5'", warning)
        self.assertIn("ccx -m gpt-6-astra", warning)
        self.assertEqual((home / "config.toml").read_text(encoding="utf-8"), before)
        self.assertIsNone(codex.model_warning(self.settings, ["exec", "-m", "gpt-6-astra", "hi"]))
        self.assertIn("'gpt-4.1' (-m)", codex.model_warning(self.settings, ["--model=gpt-4.1"]))
        self.assertIn("'gpt-5.5'", codex.model_warning(self.settings, ["exec", "--", "-m", "gpt-6-astra"]))
        codex.write_codex_home(self.settings, "x")
        self.assertIsNotNone(codex.model_warning(self.settings, []))
        (home / "config.toml").write_text(before.replace('model = "gpt-5.5"', 'model = "claude-haiku-4-5"'),
                                          encoding="utf-8")
        self.assertIsNone(codex.model_warning(self.settings, []))

    def test_find_codex_honours_override(self):
        self.assertEqual(codex.find_codex({"CCX_CODEX": "/opt/codex"}), "/opt/codex")

    def test_find_vscode_uses_the_macos_app_bundle_when_code_is_not_on_path(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            cli = Path(tmp) / "Applications" / "Visual Studio Code.app" / "Contents" / "Resources" / "app" / "bin"
            cli.mkdir(parents=True)
            (cli / "code").write_bytes(b"")
            with mock.patch.object(codex.shutil, "which", return_value=None), \
                    mock.patch.object(codex.Path, "home", return_value=Path(tmp)):
                with mock.patch.object(codex.sys, "platform", "darwin"):
                    self.assertEqual(codex.find_vscode({}), str(cli / "code"))
                    self.assertEqual(codex.find_vscode({"CCX_VSCODE": "/x/code"}), "/x/code")
                with mock.patch.object(codex.sys, "platform", "linux"), mock.patch.object(codex.os, "name", "posix"):
                    self.assertIsNone(codex.find_vscode({}))

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
            for argv in (["--respect-permissions", "--help"], ["--no-refresh", "--respect-permissions", "--version"],
                         ["--respect-permissions", "--no-refresh", "--help"]):
                with self.subTest(argv=argv):
                    self.assertEqual(claude.main(argv), 0)

    def test_claude_launcher_options_are_consumed_in_either_order(self):
        from unittest import mock
        home = TempHome()
        self.addCleanup(home.cleanup)
        settings = home.settings()
        gateway.refresh(settings, catalog())
        with mock.patch.dict("os.environ", {}, clear=True), \
                mock.patch.object(gateway, "ensure") as ensure, \
                mock.patch("copilot_gateway.settings.Settings.load", return_value=settings), \
                mock.patch.object(settings.__class__, "master_key", return_value="key"), \
                mock.patch("shutil.which", return_value="/test/claude"), \
                mock.patch.object(claude, "run", return_value=0) as run:
            for options in (["--no-refresh", "--respect-permissions"],
                            ["--respect-permissions", "--no-refresh"]):
                with self.subTest(options=options):
                    self.assertEqual(claude.main([*options, "--permission-mode", "default"]), 0)
                    ensure.assert_called_with(settings, ["claude"], do_refresh=False)
                    self.assertEqual(run.call_args.args[0],
                                     ["/test/claude", "--model", "claude-sonnet-5",
                                      "--permission-mode", "default"])
            self.assertEqual(claude.main(["--no-refresh", "--model", "claude-opus-5"]), 0)
            self.assertEqual(run.call_args.args[0],
                             ["/test/claude", "--dangerously-skip-permissions", "--model", "claude-opus-5"])

    def test_missing_executable_is_reported_not_raised(self):
        self.assertEqual(claude.run(["definitely-not-a-real-binary-ccgw"], {}), 127)


if __name__ == "__main__":
    unittest.main()
