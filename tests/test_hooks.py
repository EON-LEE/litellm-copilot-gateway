"""Codex tool filter hook (offline)."""
import asyncio
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


if __name__ == "__main__":
    unittest.main()
