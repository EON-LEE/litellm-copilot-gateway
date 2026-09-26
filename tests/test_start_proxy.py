"""Ensure a new proxy cannot advertise stale metadata when patch installation fails."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class StartupTests(unittest.TestCase):
    def test_missing_invalid_or_empty_discovery_is_not_reported_as_ready(self):
        for output in ("", "not-json", "{}", '{"data":[]}', '{"data":[{"id":"claude-sonnet-5"}]}'):
            with self.subTest(output=output), tempfile.TemporaryDirectory() as temporary:
                home = Path(temporary)
                repo = home / "litellm-copilot-gateway"
                repo.mkdir()
                shutil.copy2(ROOT / "start-proxy.sh", repo / "start-proxy.sh")
                (repo / ".env").write_text("LITELLM_MASTER_KEY=test-only-key\n")
                binary = home / ".local/bin"
                binary.mkdir(parents=True)
                for name, source in {
                    "lsof": "#!/bin/sh\nexit 0\n",
                    "curl": f"#!{sys.executable}\nprint({output!r})\n",
                }.items():
                    path = binary / name
                    path.write_text(source)
                    path.chmod(0o755)
                result = subprocess.run(["bash", str(repo / "start-proxy.sh")],
                                        env={**os.environ, "HOME": str(home)},
                                        text=True, capture_output=True, timeout=10)
                if "claude-sonnet-5" in output:
                    self.assertEqual(result.returncode, 0, result.stderr)
                else:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("readiness failed", result.stderr)

    def test_new_copilot_process_goes_through_identity_wrapper(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            repo = home / "litellm-copilot-gateway"
            repo.mkdir()
            shutil.copy2(ROOT / "start-proxy.sh", repo / "start-proxy.sh")
            (repo / ".env").write_text("LITELLM_MASTER_KEY=test-only-key\n")
            token = home / ".local/share/copilot-api/github_token"
            token.parent.mkdir(parents=True)
            token.write_text("test-only-token")
            binary = home / ".local/bin"
            binary.mkdir(parents=True)
            state = home / "capi-ready"
            trace = home / "startup-trace"
            wrapper = repo / "start-copilot-api.py"
            wrapper.write_text(f"import shutil, sys\nfrom pathlib import Path\nassert shutil.which('copilot-api') == {str(binary / 'copilot-api')!r}\nassert sys.stdin.read() == ''\nPath({str(trace)!r}).write_text('identity')\nPath({str(state)!r}).touch()\n")
            npx = '''import subprocess, sys
assert sys.argv[1:4] == ["-y", "--package", "@jeffreycao/copilot-api@2.6.15"], "Copilot API must stay pinned"
assert sys.argv[4:] == ["-c", 'python3 "$HOME/litellm-copilot-gateway/start-copilot-api.py"'], "The identity wrapper must not be bypassed"
raise SystemExit(subprocess.call(sys.argv[5], shell=True))
'''
            stubs = {
                "npx": npx,
                "copilot-api": "raise SystemExit('the identity wrapper must not be bypassed')\n",
                "lsof": f"import sys\nfrom pathlib import Path\nraise SystemExit(0 if '-iTCP:4000' in sys.argv or Path({str(state)!r}).exists() else 1)\n",
                "curl": "print('{\"data\": [{\"id\": \"claude-sonnet-5\"}]}')\n",
            }
            for name, code in stubs.items():
                script = binary / name
                script.write_text(f"#!{sys.executable}\n{code}")
                script.chmod(0o755)
            result = subprocess.run(["bash", str(repo / "start-proxy.sh")],
                                    env={**os.environ, "HOME": str(home)},
                                    text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(trace.read_text(), "identity")

    def test_new_proxy_requires_discovery_patch_before_starting(self):
        for patch_exit in (0, 1):
            with self.subTest(patch_exit=patch_exit), tempfile.TemporaryDirectory() as temporary:
                home = Path(temporary)
                repo = home / "litellm-copilot-gateway"
                repo.mkdir()
                shutil.copy2(ROOT / "start-proxy.sh", repo / "start-proxy.sh")
                (repo / ".env").write_text("LITELLM_MASTER_KEY=test-only-key\n")
                binary = home / ".local/bin"
                binary.mkdir(parents=True)
                state = home / "port-ready"
                trace = home / "startup-trace"
                stubs = {
                    "lsof": f"import sys\nfrom pathlib import Path\nraise SystemExit(0 if '-iTCP:4141' in sys.argv or Path({str(state)!r}).exists() else 1)\n",
                    "curl": f"print({json.dumps({'data': [{'id': 'claude-sonnet-5'}]})!r})\n",
                    "litellm": f"from pathlib import Path\nwith Path({str(trace)!r}).open('a') as f: f.write('start\\n')\nPath({str(state)!r}).touch()\n",
                }
                for name, code in stubs.items():
                    script = binary / name
                    script.write_text(f"#!{sys.executable}\n{code}")
                    script.chmod(0o755)
                patch = repo / "apply-patches.sh"
                patch.write_text(f"#!{sys.executable}\nfrom pathlib import Path\nwith Path({str(trace)!r}).open('a') as f: f.write('patch\\n')\nraise SystemExit({patch_exit})\n")
                patch.chmod(0o755)
                result = subprocess.run(["bash", str(repo / "start-proxy.sh")],
                                        env={**os.environ, "HOME": str(home)},
                                        text=True, capture_output=True, timeout=10)
                events = trace.read_text().splitlines() if trace.exists() else []
                if patch_exit:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(events, ["patch"])
                    self.assertFalse(state.exists())
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(events, ["patch", "start"])


class StopTests(unittest.TestCase):
    """Fake lsof/ps and intercept signals: never touch the user's processes."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="copilot-lifecycle-test-")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.repo = self.home / "litellm-copilot-gateway"
        self.repo.mkdir()
        for name in ("stop-proxy.sh", "restart-proxy.sh"):
            shutil.copy2(ROOT / name, self.repo / name)
        start = self.repo / "start-proxy.sh"
        start.write_text("#!/bin/sh\necho startup-called\n")
        start.chmod(0o755)
        self.binary = self.home / ".local/bin"
        self.binary.mkdir(parents=True)
        self.state = self.home / "listeners.json"
        self.commands = self.home / "commands.json"
        self.signals = self.home / "signals.jsonl"
        self.state.write_text("{}")
        self.commands.write_text("{}")
        package = self.home / ".npm/_npx/test/node_modules/@jeffreycao/copilot-api"
        (package / "dist").mkdir(parents=True)
        (package / "package.json").write_text(json.dumps({
            "name": "@jeffreycao/copilot-api", "bin": {"copilot-api": "./dist/main.js"},
        }))
        self.entry = package / "dist/main.js"
        self.entry.touch()
        scripts = {
            "lsof": '''
import json, os, sys
from pathlib import Path
port = next(arg.split(":")[1] for arg in sys.argv if arg.startswith("-iTCP:"))
pids = json.loads(Path(os.environ["TEST_LISTENERS"]).read_text()).get(port, [])
for pid in pids:
    print(pid)
raise SystemExit(0 if pids else 1)
''',
            "ps": '''
import json, os, sys
from pathlib import Path
pid = sys.argv[sys.argv.index("-p") + 1]
command = json.loads(Path(os.environ["TEST_COMMANDS"]).read_text()).get(pid)
if command is None:
    raise SystemExit(1)
print(command)
''',
            "python3": '''
import json, os, sys, time
from pathlib import Path
def record_signal(pid, sig):
    with Path(os.environ["TEST_SIGNALS"]).open("a") as handle:
        handle.write(json.dumps([pid, sig]) + "\\n")
    if os.environ.get("TEST_STICKY"):
        return
    state = Path(os.environ["TEST_LISTENERS"])
    listeners = json.loads(state.read_text())
    state.write_text(json.dumps({port: [p for p in pids if p != pid] for port, pids in listeners.items()}))
os.kill = record_signal
clock = iter(range(1000))
time.monotonic = lambda: next(clock)
time.sleep = lambda _: None
source = sys.stdin.read()
sys.argv = sys.argv[1:]
exec(compile(source, "<stop-proxy-test>", "exec"), {"__name__": "__main__"})
''',
            "litellm": "pass\n",
        }
        for name, source in scripts.items():
            path = self.binary / name
            path.write_text(f"#!{sys.executable}\n{source}")
            path.chmod(0o755)
        self.environment = {
            **os.environ, "HOME": str(self.home), "TEST_LISTENERS": str(self.state),
            "TEST_COMMANDS": str(self.commands), "TEST_SIGNALS": str(self.signals),
        }
        self.litellm_args = [
            str(self.binary / "litellm"), "--config", str(self.repo / "config.yaml"),
            "--host", "127.0.0.1", "--port", "4000",
        ]
        self.capi_args = ["node", str(self.entry), "start", "--port", "4141"]

    def configure(self, listeners, commands):
        self.state.write_text(json.dumps(listeners))
        self.commands.write_text(json.dumps({pid: shlex.join(args) for pid, args in commands.items()}))

    def run_script(self, name, *args):
        return subprocess.run(["bash", str(self.repo / name), *args], env=self.environment,
                              text=True, capture_output=True, timeout=10)

    def recorded_signals(self):
        return [json.loads(line) for line in self.signals.read_text().splitlines()] if self.signals.exists() else []

    def test_stop_all_signals_only_verified_gateway_listener_pids(self):
        self.configure({"4000": [10101], "4141": [20202], "9999": [30303]},
                       {"10101": self.litellm_args, "20202": self.capi_args,
                        "30303": ["node", "unrelated.js"]})
        result = self.run_script("stop-proxy.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.recorded_signals(), [[10101, 15], [20202, 15]])
        self.assertEqual(json.loads(self.state.read_text())["9999"], [30303])

    def test_restart_stops_only_litellm_before_starting(self):
        self.configure({"4000": [10101], "4141": [20202]},
                       {"10101": self.litellm_args, "20202": self.capi_args})
        result = self.run_script("restart-proxy.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.recorded_signals(), [[10101, 15]])
        self.assertEqual(json.loads(self.state.read_text())["4141"], [20202])
        self.assertIn("startup-called", result.stdout)

    def test_foreign_listener_refuses_all_signals_before_stopping_any_service(self):
        self.configure({"4000": [10101], "4141": [20202]},
                       {"10101": self.litellm_args, "20202": ["node", "other-copilot-api.js", "start"]})
        result = self.run_script("stop-proxy.sh")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no signal sent", result.stderr)
        self.assertEqual(self.recorded_signals(), [])

    def test_different_config_or_port_cannot_match_this_gateway(self):
        for index, value in ((2, str(self.repo / "config.yaml.other")), (6, "4001")):
            with self.subTest(argument=index):
                args = self.litellm_args.copy()
                args[index] = value
                self.configure({"4000": [10101]}, {"10101": args})
                result = self.run_script("restart-proxy.sh")
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.recorded_signals(), [])
                self.assertNotIn("startup-called", result.stdout)

    def test_shutdown_timeout_does_not_force_termination_or_restart(self):
        self.configure({"4000": [10101]}, {"10101": self.litellm_args})
        self.environment["TEST_STICKY"] = "1"
        result = self.run_script("restart-proxy.sh")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not forcing termination", result.stderr)
        self.assertEqual(self.recorded_signals(), [[10101, 15]])
        self.assertNotIn("startup-called", result.stdout)

    def test_no_listeners_is_idempotent(self):
        result = self.run_script("stop-proxy.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.recorded_signals(), [])
        self.assertEqual(result.stdout.count("not running"), 2)

    def test_non_positive_pid_is_never_signalled(self):
        self.configure({"4000": [0]}, {})
        result = self.run_script("stop-proxy.sh")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid listener PID", result.stderr)
        self.assertEqual(self.recorded_signals(), [])


if __name__ == "__main__":
    unittest.main()
