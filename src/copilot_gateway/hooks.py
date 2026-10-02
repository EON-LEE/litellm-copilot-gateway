"""LiteLLM proxy callbacks loaded from the generated Codex config.

Copilot's xAI models reject some Responses tool types that Codex always sends
(``namespace`` for multi-agent tools, hosted ``web_search``). The catalog marks
such models with ``model_info.unsupported_tool_types``; this hook removes only
those tool entries for those models so the request reaches the upstream.
"""
from __future__ import annotations

from litellm.integrations.custom_logger import CustomLogger

TOOL_TYPES_KEY = "unsupported_tool_types"


def _router_unsupported(model):
    try:
        from litellm.proxy.proxy_server import llm_router
    except Exception:
        return set()
    if llm_router is None or not isinstance(model, str):
        return set()
    blocked = set()
    for deployment in llm_router.get_model_list(model_name=model) or []:
        blocked.update((deployment.get("model_info") or {}).get(TOOL_TYPES_KEY) or [])
    return blocked


def strip_tools(data, blocked):
    """Return the removed tool types; mutates ``data`` in place."""
    tools = data.get("tools")
    if not blocked or not isinstance(tools, list):
        return []
    kept, removed = [], []
    for tool in tools:
        tool_type = tool.get("type") if isinstance(tool, dict) else None
        if tool_type in blocked:
            removed.append(tool_type)
        else:
            kept.append(tool)
    if not removed:
        return []
    if kept:
        data["tools"] = kept
    else:
        data.pop("tools", None)
        data.pop("tool_choice", None)
    choice = data.get("tool_choice")
    if isinstance(choice, dict) and choice.get("type") in blocked:
        data["tool_choice"] = "auto"
    return removed


class ToolFilter(CustomLogger):
    def __init__(self, lookup=_router_unsupported):
        super().__init__()
        self._lookup = lookup

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        if isinstance(data, dict):
            strip_tools(data, self._lookup(data.get("model")))
        return data


tool_filter = ToolFilter()
