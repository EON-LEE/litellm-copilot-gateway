"""Codex tool filter hook (offline)."""
import asyncio
import copy
import unittest

import helpers

from copilot_gateway import hooks

CODEX_TOOLS = [
    {"type": "function", "name": "shell"},
    {"type": "namespace", "name": "multi_agent_v1", "tools": []},
    {"type": "web_search"},
    {"type": "custom", "name": "apply_patch"},
]
BLOCKED = {"namespace", "web_search"}


class StripToolsTests(unittest.TestCase):
    def test_removes_only_blocked_types(self):
        data = {"model": "grok-4.7", "tools": [dict(t) for t in CODEX_TOOLS], "tool_choice": "auto"}
        self.assertEqual(hooks.strip_tools(data, BLOCKED), ["namespace", "web_search"])
        self.assertEqual([t["type"] for t in data["tools"]], ["function", "custom"])
        self.assertEqual(data["tool_choice"], "auto")

    def test_no_blocked_types_leaves_request_untouched(self):
        data = {"model": "gpt-5.5", "tools": [dict(t) for t in CODEX_TOOLS]}
        self.assertEqual(hooks.strip_tools(data, set()), [])
        self.assertEqual(data["tools"], CODEX_TOOLS)

    def test_drops_empty_tools_and_forced_choice(self):
        data = {"tools": [{"type": "web_search"}], "tool_choice": {"type": "web_search"}}
        hooks.strip_tools(data, BLOCKED)
        self.assertNotIn("tools", data)
        self.assertNotIn("tool_choice", data)
        data = {"tools": [{"type": "web_search"}, {"type": "function", "name": "f"}],
                "tool_choice": {"type": "web_search"}}
        hooks.strip_tools(data, BLOCKED)
        self.assertEqual(data["tool_choice"], "auto")

    def test_hook_uses_per_model_lookup(self):
        lookup = {"grok-4.7": BLOCKED}.get
        hook = hooks.ToolFilter(lambda model: lookup(model) or set())
        grok = {"model": "grok-4.7", "tools": [dict(t) for t in CODEX_TOOLS]}
        gpt = {"model": "gpt-5.5", "tools": [dict(t) for t in CODEX_TOOLS]}
        for data in (grok, gpt):
            asyncio.run(hook.async_pre_call_hook(None, None, data, "aresponses"))
        self.assertEqual(len(grok["tools"]), 2)
        self.assertEqual(len(gpt["tools"]), 4)

    def test_router_lookup_reads_model_info(self):
        result = helpers.run_python(
            "import litellm\n"
            "from litellm.proxy import proxy_server\n"
            "from litellm.proxy.types_utils.utils import get_instance_fn\n"
            "proxy_server.llm_router = litellm.Router(model_list=[\n"
            " {'model_name':'grok-4.7','litellm_params':{'model':'openai/grok-4.7','api_key':'x'},\n"
            "  'model_info':{'unsupported_tool_types':['namespace','web_search']}},\n"
            " {'model_name':'gpt-5.5','litellm_params':{'model':'openai/gpt-5.5','api_key':'x'}}])\n"
            "hook = get_instance_fn('copilot_gateway.hooks.tool_filter')\n"
            "from copilot_gateway import hooks\n"
            "assert isinstance(hook, hooks.ToolFilter)\n"
            "print(sorted(hooks._router_unsupported('grok-4.7')), sorted(hooks._router_unsupported('gpt-5.5')))\n")
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertIn("['namespace', 'web_search'] []", result.stdout)


class ForeignFunctionIdTests(unittest.TestCase):
    def setUp(self):
        self.foreign = {"type": "function_call", "id": "call_foreign", "call_id": "call_foreign",
                        "name": "shell", "arguments": '{"cmd":"pwd"}'}
        self.output = {"type": "function_call_output", "id": "fco_original",
                       "call_id": "call_foreign", "output": "ok"}

    def test_only_duplicate_foreign_item_id_is_removed(self):
        good = {"type": "function_call", "id": "fc_native", "call_id": "call_native",
                "name": "shell", "arguments": "{}"}
        unusual = {"type": "function_call", "id": "call_other", "call_id": "call_different"}
        missing = {"type": "function_call", "call_id": "call_missing"}
        data = {"input": [copy.deepcopy(self.foreign), copy.deepcopy(self.output), good, unusual, missing,
                          {"type": "message", "id": "call_foreign"}, "malformed"]}
        original = copy.deepcopy(data)
        hooks.strip_foreign_function_ids(data)
        self.assertNotIn("id", data["input"][0])
        self.assertEqual(data["input"][0]["call_id"], data["input"][1]["call_id"])
        self.assertEqual(data["input"][0]["arguments"], original["input"][0]["arguments"])
        self.assertEqual(data["input"][1:], original["input"][1:])
        for value in (None, "conversation-id", {}, []):
            hooks.strip_foreign_function_ids({"input": value})

    def test_applies_only_to_openai_responses(self):
        hook = hooks.ToolFilter(lambda _: set(), lambda model: model == "gpt")
        for model, call_type, should_strip in (("gpt", "aresponses", True),
                                               ("gpt", "acompletion", False),
                                               ("gemini", "aresponses", False)):
            with self.subTest(model=model, call_type=call_type):
                data = {"model": model, "input": [copy.deepcopy(self.foreign), copy.deepcopy(self.output)]}
                asyncio.run(hook.async_pre_call_hook(None, None, data, call_type))
                self.assertEqual("id" not in data["input"][0], should_strip)
                self.assertEqual(data["input"][1], self.output)

    def test_route_lookup_uses_live_router_and_requires_all_deployments_to_match(self):
        result = helpers.run_python(
            "import litellm\n"
            "from litellm.proxy import proxy_server\n"
            "from copilot_gateway import hooks\n"
            "proxy_server.llm_router = litellm.Router(model_list=[\n"
            " {'model_name':'gpt','litellm_params':{'model':'openai/gpt','api_key':'x'},"
            "  'model_info':{'upstream_vendor':'OpenAI'}},\n"
            " {'model_name':'grok','litellm_params':{'model':'openai/grok','api_key':'x'},"
            "  'model_info':{'upstream_vendor':'xAI'}},\n"
            " {'model_name':'claude','litellm_params':{'model':'anthropic/claude','api_key':'x'},"
            "  'model_info':{'upstream_vendor':'Anthropic'}}])\n"
            "print(hooks._router_openai_responses('gpt'), hooks._router_openai_responses('grok'),"
            " hooks._router_openai_responses('claude'), hooks._router_openai_responses('missing'))\n")
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertIn("True False False False", result.stdout)


if __name__ == "__main__":
    unittest.main()
