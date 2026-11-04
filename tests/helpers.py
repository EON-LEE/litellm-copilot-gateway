"""Shared offline fixtures (no network, no real tokens, no real services)."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")


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
        model("trajectory-compaction", "Internal compaction", 256000, 245760, 10240, ["/chat/completions"],
              model_picker_enabled=False),
    ]
    for row in rows[:3]:
        row["capabilities"]["limits"]["max_non_streaming_output_tokens"] = 16000
    rows.append({"id": "text-embedding-3-small", "model_picker_enabled": True, "capabilities": {"type": "embeddings"}})
    return {"object": "list", "data": rows}


class TempHome:
    """A throwaway CCGW_HOME with distinct test ports; returns loaded Settings."""

    def __init__(self, **extra):
        self.temp = tempfile.TemporaryDirectory(prefix="ccgw-test-")
        self.path = Path(self.temp.name)
        self.environ = {"CCGW_HOME": str(self.path), "CCGW_PORT": "24000", "CCGW_CODEX_PORT": "24001",
                        "CCGW_CAPI_PORT": "24141", "GITHUB_COPILOT_TOKEN_DIR": str(self.path / "tok"), **extra}

    def settings(self):
        from copilot_gateway.settings import Settings
        return Settings.load(self.environ)

    def cleanup(self):
        self.temp.cleanup()


def run_python(code, timeout=120):
    """Run code in a fresh interpreter (import hooks must precede LiteLLM imports)."""
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "LITELLM_LOCAL_MODEL_COST_MAP": "True",
           "PYTHONPATH": str(SRC) + os.pathsep + os.environ.get("PYTHONPATH", ""), "PYTHONUTF8": "1"}
    return subprocess.run([sys.executable, "-c", code], env=env, text=True, capture_output=True,
                          timeout=timeout, encoding="utf-8")
