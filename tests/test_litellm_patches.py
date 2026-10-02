"""In-memory LiteLLM patches P1–P6 (offline; real LiteLLM is imported only in a subprocess)."""
import json
import unittest

from helpers import run_python

from copilot_gateway import litellm_patches as lp


class AnchorTests(unittest.TestCase):
    def test_installed_litellm_accepts_every_patch(self):
        self.assertEqual(lp.verify(), ["P1", "P2", "P3", "P4", "P5", "P6"])

    def test_each_patch_fails_closed_when_upstream_changes(self):
        for fullname, patches in lp.PATCHES.items():
            original = lp.module_source(fullname)
            for patch in patches:
                with self.subTest(patch=patch.__name__):
                    patched = patch(original)
                    self.assertIn("PATCHED (ccgw)", patched)
                    with self.assertRaises(lp.PatchError):
                        patch("def unrelated():\n    return None\n")

    def test_install_refuses_after_target_already_imported(self):
        result = run_python("import litellm.proxy.utils\n"
                            "from copilot_gateway import litellm_patches as lp\n"
                            "try:\n    lp.install()\nexcept lp.PatchError as e:\n    print('refused', e)\n")
        self.assertIn("refused", result.stdout, result.stderr)


def message(text, role="assistant"):
    return {"type": "message", "role": role, "content": [{"type": "output_text", "text": text}]}


def call(name):
    return {"type": "function_call", "call_id": f"c-{name}", "name": name, "arguments": "{}"}


def output(name):
    return {"type": "function_call_output", "call_id": f"c-{name}", "output": "ok"}


class NormalizeTests(unittest.TestCase):
    def test_empty_assistant_messages(self):
        for item in (message(""), message("  "), {"role": "assistant", "content": ""},
                     {"role": "assistant", "content": None}, {"role": "assistant", "content": []}):
            self.assertTrue(lp.is_empty_assistant_item(item), item)
        for item in (message("hi"), message("", role="user"), call("x"), "text",
                     {"role": "assistant", "content": [{"type": "refusal", "refusal": ""}]}):
            self.assertFalse(lp.is_empty_assistant_item(item), item)

    def test_codex_turn_is_reordered_text_before_tool_calls(self):
        reasoning = {"type": "reasoning", "summary": []}
        items = [message("q", "user"), call("a"), reasoning, message("Reading"), message(""),
                 output("a"), call("b"), message("Done")]
        self.assertEqual(lp.normalize_assistant_items(items),
                         [message("q", "user"), reasoning, message("Reading"), call("a"), output("a"),
                          message("Done"), call("b")])

    def test_user_and_tool_outputs_are_barriers(self):
        items = [call("a"), output("a"), message("after")]
        self.assertEqual(lp.normalize_assistant_items(items), items)


PROBE = r'''
import json
from copilot_gateway import litellm_patches as lp
lp.install()
from litellm.responses.litellm_completion_transformation.transformation import LiteLLMCompletionResponsesConfig
from litellm.responses.litellm_completion_transformation import streaming_iterator as si
from litellm.llms.anthropic.experimental_pass_through.adapters.transformation import (
    LiteLLMAnthropicMessagesAdapter)
from litellm.llms.github_copilot import authenticator
import inspect
from litellm import Router
import litellm.proxy.utils as utils

out = {}
items = [
    {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "list files"}]},
    {"type": "function_call", "call_id": "c1", "name": "shell", "arguments": "{}"},
    {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": ""}]},
    {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Listing"}]},
    {"type": "function_call_output", "call_id": "c1", "output": "a.txt"},
]
msgs = LiteLLMCompletionResponsesConfig.transform_responses_api_input_to_messages(
    input=items, responses_api_request={})
out["p5"] = json.loads(json.dumps(msgs, default=lambda o: getattr(o, "model_dump", lambda: str(o))()))

cls = si.LiteLLMCompletionStreamingIterator
out["p6"] = all(hasattr(cls, n) for n in ("create_output_item_added_event", "create_content_part_added_event"))
import sys
out["loaders"] = {name: type(sys.modules[name].__spec__.loader).__name__ for name in lp.PATCHES}

info = {"display_name": "Copilot Test", "description": "ctx", "gateway_provider": "github_copilot",
        "upstream_model_id": "test-model", "max_context_window_tokens": 128000, "api_key": "secret",
        "cache_creation_input_token_cost": 0, "cache_read_input_token_cost": 0}
params = {"model": "openai/gpt-4o-mini", "api_base": "http://127.0.0.1:1", "api_key": "x"}
router = Router(model_list=[{"model_name": "copilot", "litellm_params": params, "model_info": info},
                            {"model_name": "plain", "litellm_params": params}])
cost = lambda _: {"mode": "chat", "max_input_tokens": 1000, "max_output_tokens": 500}
out["p4"] = {n: utils.create_model_info_response(n, "openai", llm_router=router, get_model_info=cost)
             for n in ("copilot", "plain")}
print("JSON" + json.dumps(out))
'''


class ImportedPatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        result = run_python(PROBE)
        line = next((l for l in result.stdout.splitlines() if l.startswith("JSON")), None)
        if result.returncode != 0 or line is None:
            raise AssertionError(result.stdout[-2000:] + result.stderr[-4000:])
        cls.out = json.loads(line[4:])

    def test_every_target_module_was_loaded_through_the_patch_loader(self):
        self.assertTrue(self.out["p6"])
        self.assertEqual(set(self.out["loaders"].values()), {"_PatchedLoader"}, self.out["loaders"])

    def test_p5_codex_history_becomes_claude_safe(self):
        dumped = json.dumps(self.out["p5"])
        self.assertNotIn("Empty message content", dumped)
        assistants = [m for m in self.out["p5"] if m.get("role") == "assistant"]
        self.assertTrue(assistants)
        # Text precedes the tool call: either one merged message or text message first.
        first = assistants[0]
        self.assertTrue(first.get("content"), self.out["p5"])
        self.assertIn("Listing", json.dumps(first.get("content")))
        self.assertEqual(self.out["p5"][-1].get("role"), "tool")

    def test_p4_exposes_only_allowlisted_copilot_metadata(self):
        copilot, plain = self.out["p4"]["copilot"], self.out["p4"]["plain"]
        self.assertEqual((copilot["display_name"], copilot["owned_by"], copilot["upstream_model_id"]),
                         ("Copilot Test", "github_copilot", "test-model"))
        self.assertEqual(copilot["max_context_window_tokens"], 128000)
        self.assertNotIn("secret", json.dumps(copilot))
        self.assertNotIn("gateway_provider", copilot)
        self.assertEqual(plain["owned_by"], "openai")
        self.assertNotIn("display_name", plain)


if __name__ == "__main__":
    unittest.main()
