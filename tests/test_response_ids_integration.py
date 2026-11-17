"""Offline Responses requests through a real LiteLLM proxy and a fake upstream."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import helpers


class ProxyIntegrationTests(unittest.TestCase):
    def test_foreign_tool_call_id_preserves_call_output_pairing(self):
        requests = []

        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(data)
                calls = [item for item in data["input"] if item.get("type") == "function_call"]
                invalid = any(item.get("id", "").startswith("call_") for item in calls)
                body = ({"error": {"message": "invalid function call id", "type": "invalid_request_error"}}
                        if invalid else {"id": "resp_mock", "object": "response", "created_at": 1,
                                         "status": "completed", "model": "gpt-test", "output": [],
                                         "usage": {"input_tokens": 1, "output_tokens": 0, "total_tokens": 1}})
                streaming = data.get("stream") is True and not invalid
                payload = (f"data: {json.dumps({'type': 'response.completed', 'response': body})}\n\n"
                           "data: [DONE]\n\n").encode() if streaming else json.dumps(body).encode()
                self.send_response(400 if invalid else 200)
                self.send_header("Content-Type", "text/event-stream" if streaming else "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        self.addCleanup(upstream.server_close)
        self.addCleanup(upstream.shutdown)

        with tempfile.TemporaryDirectory() as temp:
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            config = {
                "model_list": [{
                    "model_name": "gpt-test",
                    "litellm_params": {"model": "openai/gpt-test", "api_key": "test-key",
                                       "api_base": f"http://127.0.0.1:{upstream.server_port}/v1"},
                    "model_info": {"gateway_provider": "github_copilot", "upstream_vendor": "OpenAI"},
                }],
                "litellm_settings": {"callbacks": ["copilot_gateway.hooks.tool_filter"]},
                "general_settings": {"master_key": "test-master-key"},
            }
            config_path = Path(temp) / "config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            env = {**os.environ, "PYTHONPATH": str(helpers.SRC) + os.pathsep + os.environ.get("PYTHONPATH", ""),
                   "LITELLM_LOCAL_MODEL_COST_MAP": "True", "PYTHONDONTWRITEBYTECODE": "1",
                   "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}
            with tempfile.TemporaryFile(mode="w+b") as log:
                process = subprocess.Popen([sys.executable, "-m", "copilot_gateway.litellm_runner",
                                            "--config", str(config_path), "--port", str(port)],
                                           env=env, stdout=log, stderr=subprocess.STDOUT)
                try:
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    base = f"http://127.0.0.1:{port}/v1"
                    headers = {"Authorization": "Bearer test-master-key", "Content-Type": "application/json"}
                    deadline = time.monotonic() + 120
                    while True:
                        if process.poll() is not None:
                            log.seek(0)
                            self.fail("proxy exited during startup: " + log.read().decode("utf-8", "replace")[-3000:])
                        try:
                            with opener.open(urllib.request.Request(base + "/models", headers=headers), timeout=1):
                                break
                        except (OSError, urllib.error.URLError):
                            if time.monotonic() >= deadline:
                                self.fail("proxy did not become ready")
                            time.sleep(0.2)
                    items = [
                        {"type": "function_call", "id": "call_foreign", "call_id": "call_foreign",
                         "name": "shell", "arguments": "{}"},
                        {"type": "function_call_output", "id": "fco_original",
                         "call_id": "call_foreign", "output": "ok"},
                        {"type": "function_call", "id": "fc_native", "call_id": "call_native",
                         "name": "shell", "arguments": "{}"},
                    ]
                    for stream in (False, True):
                        request = urllib.request.Request(
                            base + "/responses", headers=headers, method="POST",
                            data=json.dumps({"model": "gpt-test", "input": items, "stream": stream}).encode())
                        with opener.open(request, timeout=30) as response:
                            self.assertEqual(response.status, 200)
                            result = response.read().decode()
                        if stream:
                            self.assertIn("response.completed", result)
                    self.assertEqual(len(requests), 2)
                    for received in requests:
                        sent = received["input"]
                        self.assertNotIn("id", sent[0])
                        self.assertEqual(sent[0]["call_id"], sent[1]["call_id"])
                        self.assertEqual(sent[1:], items[1:])
                    self.assertEqual(items[0]["id"], "call_foreign")
                finally:
                    process.terminate()
                    process.wait(timeout=30)


if __name__ == "__main__":
    unittest.main()
