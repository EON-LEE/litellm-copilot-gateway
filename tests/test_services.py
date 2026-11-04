"""Port-ownership safety: only verified gateway PIDs are ever signalled (cross-platform)."""
import socket
import subprocess
import sys
import time
import unittest
from pathlib import Path

from helpers import TempHome

from copilot_gateway import services

LISTEN = ("import socket,sys,time;s=socket.socket();s.bind(('127.0.0.1',int(sys.argv[3])));"
          "s.listen();time.sleep(120)")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ClassifyTests(unittest.TestCase):
    def setUp(self):
        self.config = Path("/data/codex.yaml")
        self.svc = services.Service("codex", 24001, Path("x.log"), "litellm", config=self.config)
        self.capi = services.Service("capi", 24141, Path("y.log"), "capi")

    def test_ours_requires_matching_port_and_config(self):
        ours = ["python", "-m", "copilot_gateway.litellm_runner", "--config", str(self.config), "--port", "24001"]
        self.assertEqual(self.svc.classify(ours), "ours")
        self.assertIsNone(self.svc.classify(ours[:-1] + ["4001"]))
        self.assertIsNone(self.svc.classify(ours[:4] + ["/other.yaml"] + ours[5:]))
        self.assertIsNone(self.svc.classify(["node", "server.js", "--port", "24001"]))
        self.assertIsNone(self.svc.classify(None))

    def test_capi_requires_pinned_bundle_and_port(self):
        main = r"C:\d\copilot-api\2.6.15\node_modules\@jeffreycao\copilot-api\dist\main.js"
        self.assertEqual(self.capi.classify(["node", main, "start", "--port", "24141"]), "ours")
        self.assertIsNone(self.capi.classify(["node", main, "start", "--port", "4141"]))
        self.assertIsNone(self.capi.classify(["node", "other/main.js", "start", "--port", "24141"]))

    def test_legacy_bash_gateway_is_recognised_for_claude_only(self):
        legacy = ["/home/u/.local/bin/litellm", "--config", str(services.LEGACY_REPO / "config.yaml"),
                  "--port", "4000"]
        claude = services.Service("claude", 4000, Path("z.log"), "litellm", config=Path("/new/config.yaml"))
        self.assertEqual(claude.classify(legacy), "legacy")
        self.assertIsNone(services.Service("codex", 4000, Path("z.log"), "litellm",
                                           config=Path("/c.yaml")).classify(legacy))


class StopTests(unittest.TestCase):
    def setUp(self):
        self.home = TempHome()
        self.addCleanup(self.home.cleanup)
        self.port = free_port()
        self.config = self.home.path / "codex.yaml"
        self.service = services.Service("codex", self.port, self.home.path / "codex.log", "litellm",
                                        config=self.config)

    def listen(self, *extra):
        proc = subprocess.Popen([sys.executable, "-c", LISTEN, *extra],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: proc.poll() is None and (proc.terminate(), proc.wait(10)))
        deadline = time.monotonic() + 15
        while not services.port_open(self.port) and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertTrue(services.port_open(self.port))
        return proc

    def test_stopped_port(self):
        self.assertEqual(services.inspect(self.service).status, "stopped")
        self.assertFalse(services.stop(self.service, log=lambda *_: None))

    def test_foreign_listener_is_never_signalled(self):
        proc = self.listen("not-ours", "--port", str(self.port))
        self.assertEqual(services.inspect(self.service).status, "foreign")
        with self.assertRaises(services.ServiceError):
            services.stop(self.service, log=lambda *_: None)
        with self.assertRaises(services.ServiceError):
            services.start(self.service, log=lambda *_: None)
        self.assertIsNone(proc.poll())

    def test_verified_gateway_listener_is_stopped(self):
        # argv[3] is the port for LISTEN; the rest mirrors the real runner command line.
        proc = self.listen("copilot_gateway.litellm_runner", "--port", str(self.port), "--config", str(self.config))
        self.assertEqual(services.inspect(self.service).status, "running")
        self.assertTrue(services.stop(self.service, log=lambda *_: None))
        proc.wait(10)
        self.assertFalse(services.port_open(self.port))

    def test_startup_timeout_terminates_the_spawned_child(self):
        marker = self.home.path / "child.pid"
        code = f"import os,time;open({str(marker)!r},'w').write(str(os.getpid()));time.sleep(120)"
        service = services.Service("codex", self.port, self.home.path / "codex.log", "litellm", config=self.config,
                                   command=lambda: [sys.executable, "-c", code], startup_timeout=3)
        with self.assertRaises(services.ServiceError):
            services.start(service, log=lambda *_: None)
        self.assertFalse(services.psutil.pid_exists(int(marker.read_text())))


if __name__ == "__main__":
    unittest.main()
