"""LiteLLM proxy callbacks loaded from the generated Codex config.

Copilot's xAI models reject some Responses tool types that Codex always sends
(``namespace`` for multi-agent tools, hosted ``web_search``). The catalog marks
such models with ``model_info.unsupported_tool_types``; this hook removes only
those tool entries for those models so the request reaches the upstream.

OpenAI Responses rejects another provider's ``call_`` item ID, even though its
``call_id`` is valid. Omit only the optional item ID in that case.
"""
from __future__ import annotations

from litellm.integrations.custom_logger import CustomLogger

TOOL_TYPES_KEY = "unsupported_tool_types"


def _router_models(model):
    try:
        from litellm.proxy.proxy_server import llm_router
    except Exception:
        return []
    if llm_router is None or not isinstance(model, str):
        return []
    return llm_router.get_model_list(model_name=model) or []


def _router_unsupported(model):
    blocked = set()
    for deployment in _router_models(model):
        blocked.update((deployment.get("model_info") or {}).get(TOOL_TYPES_KEY) or [])
    return blocked


def _router_openai_responses(model):
    deployments = _router_models(model)
    return bool(deployments) and all(
        (row.get("model_info") or {}).get("upstream_vendor") == "OpenAI"
        and (row.get("litellm_params") or {}).get("model", "").startswith("openai/")
        for row in deployments
    )


def strip_foreign_function_ids(data):
    """Keep the call/output pairing intact; remove only duplicated foreign item IDs."""
    items = data.get("input")
    if not isinstance(items, list):
        return
    for item in items:
        if (isinstance(item, dict) and item.get("type") == "function_call"
                and isinstance(item.get("id"), str) and item["id"].startswith("call_")
                and item["id"] == item.get("call_id")):
            del item["id"]


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
    def __init__(self, lookup=_router_unsupported, openai_lookup=_router_openai_responses):
        super().__init__()
        self._lookup = lookup
        self._openai_lookup = openai_lookup

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        if isinstance(data, dict):
            strip_tools(data, self._lookup(data.get("model")))
            if call_type == "aresponses" and self._openai_lookup(data.get("model")):
                strip_foreign_function_ids(data)
        return data


tool_filter = ToolFilter()
