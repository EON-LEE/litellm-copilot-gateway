"""Check launcher defaults and argument forwarding without starting services."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.repo = self.home / "litellm-copilot-gateway"
        self.repo.mkdir()
        shutil.copy2(ROOT / "claude-copilot.sh", self.repo / "claude-copilot.sh")
        (self.repo / ".env").write_text("LITELLM_MASTER_KEY=test-only-key\n")
        (self.repo / "config.yaml").write_text("model_list: []\n")
        for name in ("refresh-models.sh", "restart-proxy.sh", "start-proxy.sh"):
            script = self.repo / name
            script.write_text("#!/bin/sh\nexit 0\n")
            script.chmod(0o755)
        self.bin = self.home / "bin"
        self.bin.mkdir()
        claude = self.bin / "claude"
        claude.write_text(f'''#!{sys.executable}
import json, os, sys
print(json.dumps({{"argv": sys.argv[1:], "env": {{k:v for k,v in os.environ.items() if k.startswith("ANTHROPIC_") and k != "ANTHROPIC_AUTH_TOKEN"}}}}))
''')
        claude.chmod(0o755)
        curl = self.bin / "curl"
        curl.write_text("#!/bin/sh\nexit 0\n")
        curl.chmod(0o755)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("ANTHROPIC_")}
        self.env.update(HOME=str(self.home), PATH=f"{self.bin}:{os.environ['PATH']}")

    def run_launcher(self, *args):
        return subprocess.run(["bash", str(self.repo / "claude-copilot.sh"), *args],
                              env=self.env, text=True, capture_output=True, timeout=10)

    def test_haiku_default_is_haiku_and_small_fast_is_explicit_gpt(self):
        result = self.run_launcher()
        self.assertEqual(result.returncode, 0, result.stderr)
        launched = json.loads(result.stdout)
        self.assertEqual(launched["env"]["ANTHROPIC_DEFAULT_HAIKU_MODEL"], "claude-haiku-4.5")
        self.assertEqual(launched["env"]["ANTHROPIC_SMALL_FAST_MODEL"], "gpt-5-mini")
        self.assertEqual(launched["argv"], ["--dangerously-skip-permissions", "--model", "claude-sonnet-5"])

    def test_explicit_model_is_not_shadowed_by_a_default(self):
        for args in (("--model", "gpt-6-astra"), ("--model=gpt-6-astra",)):
            with self.subTest(args=args):
                result = self.run_launcher(*args)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["argv"], ["--dangerously-skip-permissions", *args])

    def test_user_default_overrides_are_preserved(self):
        self.env.update(ANTHROPIC_MODEL="gpt-6-astra", ANTHROPIC_DEFAULT_HAIKU_MODEL="claude-haiku-4.5")
        result = self.run_launcher()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["argv"][-1], "gpt-6-astra")

    def test_prompt_after_option_separator_cannot_override_default_model(self):
        result = self.run_launcher("--", "--model=gpt-6-astra")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["argv"],
                         ["--dangerously-skip-permissions", "--model", "claude-sonnet-5",
                          "--", "--model=gpt-6-astra"])

    def test_refresh_failure_uses_existing_config_but_not_a_missing_one(self):
        (self.repo / "refresh-models.sh").write_text("#!/bin/sh\nexit 1\n")
        existing = self.run_launcher()
        self.assertEqual(existing.returncode, 0, existing.stderr)
        (self.repo / "config.yaml").unlink()
        missing = self.run_launcher()
        self.assertNotEqual(missing.returncode, 0)
        self.assertEqual(missing.stdout, "")

    def test_refresh_failure_still_checks_both_services_before_launching(self):
        (self.repo / "refresh-models.sh").write_text("#!/bin/sh\nexit 1\n")
        started = self.home / "start-checked"
        (self.repo / "start-proxy.sh").write_text(f"#!/bin/sh\ntouch {str(started)!r}\nexit 1\n")
        result = self.run_launcher()
        self.assertTrue(started.exists())
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
