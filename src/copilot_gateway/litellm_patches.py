"""In-memory LiteLLM source patches, applied at import time.

The previous bash setup rewrote files in LiteLLM's site-packages, which an
upgrade silently wiped. Here a meta-path finder intercepts only the target
modules, applies exact anchor replacements to their source in memory and
compiles that (bypassing the bytecode cache). An anchor mismatch raises
PatchError, so an unverified LiteLLM never serves traffic with a missing fix.
"""
from __future__ import annotations

import ast
import importlib.abc
import importlib.machinery
import importlib.util
import re
import sys


class PatchError(RuntimeError):
    pass


def _replace(source, old, new, count, label):
    found = source.count(old)
    if found != count:
        raise PatchError(f"{label}: expected {count} anchor match(es), found {found}; LiteLLM may have changed")
    return source.replace(old, new)


def device_login_window(source):
    """P1: device-login polling 12×5s → 120×5s (humans need time to type the code)."""
    pattern = re.compile(r"^(\s*)max_attempts = 12\b.*$", re.MULTILINE)
    if len(pattern.findall(source)) != 1:
        raise PatchError("P1 device-login window: anchor not found exactly once; LiteLLM may have changed")
    return pattern.sub(r"\1max_attempts = 120  # PATCHED (ccgw): 10 minute device-login window", source)


def dangling_tool_choice(source):
    """P2: drop a forced tool_choice when Anthropic→OpenAI tool translation leaves no tools.

    Claude Code's WebSearch has only a hosted tool; github_copilot backends 400
    with "tools are required when tool choice is specified" otherwise.
    """
    return _replace(source, """        if not regular_tools:
            return {}
""", """        if not regular_tools:
            new_kwargs.pop("tool_choice", None)  # PATCHED (ccgw): drop dangling forced tool_choice
            return {}
""", 1, "P2 dangling tool_choice")


def empty_choices(source):
    """P3: Copilot's final usage-only chunk has choices=[]; don't crash the Anthropic SSE adapter."""
    source = _replace(source, """        if chunk.choices[0].finish_reason is not None:
            return False
""", """        if not chunk.choices:  # PATCHED (ccgw): guard empty choices (usage-only chunk)
            return False
        if chunk.choices[0].finish_reason is not None:
            return False
""", 1, "P3a empty choices")
    return _replace(source, '''                if chunk == "None" or chunk is None:
                    raise Exception
''', '''                if chunk == "None" or chunk is None:
                    raise Exception

                # PATCHED (ccgw): usage-only chunk with empty choices. Everything
                # below dereferences chunk.choices[0]; merge its usage into the
                # held message_delta if one is pending, otherwise drop it.
                if not getattr(chunk, "choices", None):
                    if self.holding_stop_reason_chunk is not None and getattr(chunk, "usage", None) is not None:
                        merged_chunk = self._merge_usage_into_held_stop_reason_chunk(chunk)
                        self.chunk_queue.append(merged_chunk)
                        self.queued_usage_chunk = True
                        self.holding_stop_reason_chunk = None
                        return self.chunk_queue.popleft()
                    continue
''', 2, "P3b usage-only chunk (sync + async)")


DISCOVERY_ANCHOR = """    if not include_metadata:
        return base
"""
DISCOVERY_PATCH = """    # PATCHED (ccgw): expose configured Copilot discovery metadata
    if llm_router is not None:
        configured_models = llm_router.get_model_list(model_name=model_id) or []
        if len(configured_models) == 1:
            configured_info = configured_models[0].get("model_info") or {}
            if configured_info.get("gateway_provider") == "github_copilot":
                for source, target in (
                    ("display_name", "display_name"),
                    ("description", "description"),
                    ("gateway_provider", "owned_by"),
                    ("upstream_model_id", "upstream_model_id"),
                    ("max_context_window_tokens", "max_context_window_tokens"),
                    ("max_non_streaming_output_tokens", "max_non_streaming_output_tokens"),
                ):
                    value = configured_info.get(source)
                    if value is not None:
                        base[target] = value

""" + DISCOVERY_ANCHOR


def discovery_metadata(source):
    """P4: /v1/models exposes allowlisted Copilot model_info (labels, context) for Claude Code's picker."""
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        raise PatchError(f"P4 discovery metadata: cannot parse source: {error}") from None
    functions = [node for node in tree.body
                 if isinstance(node, ast.FunctionDef) and node.name == "create_model_info_response"]
    if len(functions) != 1:
        raise PatchError(f"P4 discovery metadata: expected 1 create_model_info_response, found {len(functions)}")
    lines = source.splitlines(keepends=True)
    start, end = functions[0].lineno - 1, functions[0].end_lineno
    block = "".join(lines[start:end])
    block = _replace(block, DISCOVERY_ANCHOR, DISCOVERY_PATCH, 1, "P4 discovery metadata")
    return "".join(lines[:start]) + block + "".join(lines[end:])


EMPTY_ASSISTANT_ANCHOR = """        messages.extend(
            LiteLLMCompletionResponsesConfig._transform_response_input_param_to_chat_completion_message(
                input=input,
            )
        )
"""


def is_empty_assistant_item(item):
    """True for a Responses input assistant message carrying no text at all."""
    if not isinstance(item, dict) or item.get("type", "message") != "message" or item.get("role") != "assistant":
        return False
    content = item.get("content")
    if content is None or isinstance(content, str):
        return not (content or "").strip()
    if not isinstance(content, list):
        return False
    return all(isinstance(part, dict) and part.get("type") in ("output_text", "input_text", "text")
               and not (part.get("text") or "").strip() for part in content)


def _is_assistant_message(item):
    return isinstance(item, dict) and item.get("type", "message") == "message" and item.get("role") == "assistant"


def normalize_assistant_items(items):
    """Drop empty assistant messages and put assistant text before same-turn tool calls.

    Codex records a turn as function_call then message (stream order), which
    converts to an assistant [tool_use, text] block; Claude treats text after
    tool_use as an assistant prefill and rejects the request.
    """
    result, run = [], []

    def flush():
        result.extend(item for item in run if item.get("type") == "reasoning")
        result.extend(item for item in run if _is_assistant_message(item))
        result.extend(item for item in run if item.get("type") == "function_call")
        run.clear()

    for item in items:
        if is_empty_assistant_item(item):
            continue
        kind = item.get("type") if isinstance(item, dict) else None
        if _is_assistant_message(item) or kind in ("function_call", "reasoning"):
            run.append(item)
        else:
            flush()
            result.append(item)
    flush()
    return result


def empty_assistant_items(source):
    """P5: normalize Codex assistant history for Claude (no empty / trailing assistant text)."""
    return _replace(source, EMPTY_ASSISTANT_ANCHOR, """        # PATCHED (ccgw): normalize Codex assistant history (Claude prefill 400)
        if isinstance(input, list):
            from copilot_gateway.litellm_patches import normalize_assistant_items
            input = normalize_assistant_items(input)
""" + EMPTY_ASSISTANT_ANCHOR, 1, "P5 assistant item order")


def late_message_item(source):
    """P6: open the message item before the first text delta when the stream began with reasoning/tool calls.

    LiteLLM only emits output_item.added for the first chunk's kind, so text
    after a reasoning or tool-call start had no active item and Codex dropped
    it ("OutputTextDelta without active item").
    """
    return _replace(source, """        delta_content = self._get_delta_string_from_streaming_choices(chunk.choices)
        if delta_content:
            self._sequence_number += 1
""", """        delta_content = self._get_delta_string_from_streaming_choices(chunk.choices)
        if delta_content:
            if not self.sent_content_part_added_event:  # PATCHED (ccgw): open the message item late
                self._cached_item_id = item_id
                self.sent_content_part_added_event = True
                self._pending_response_events.append(self.create_output_item_added_event())
                self._pending_response_events.append(self.create_content_part_added_event())
            self._sequence_number += 1
""", 1, "P6 late message item")


PATCHES = {
    "litellm.responses.litellm_completion_transformation.streaming_iterator": (late_message_item,),
    "litellm.responses.litellm_completion_transformation.transformation": (empty_assistant_items,),
    "litellm.llms.github_copilot.authenticator": (device_login_window,),
    "litellm.llms.anthropic.experimental_pass_through.adapters.transformation": (dangling_tool_choice,),
    "litellm.llms.anthropic.experimental_pass_through.adapters.streaming_iterator": (empty_choices,),
    "litellm.proxy.utils": (discovery_metadata,),
}


def patched_source(fullname, source):
    for patch in PATCHES[fullname]:
        source = patch(source)
    compile(source, fullname, "exec", dont_inherit=True)
    return source


class _PatchedLoader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):
        source = importlib.util.decode_source(self.get_data(self.path))
        return compile(patched_source(fullname, source), self.path, "exec", dont_inherit=True)


class _PatchFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname not in PATCHES:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or not isinstance(spec.loader, importlib.machinery.SourceFileLoader):
            raise PatchError(f"{fullname}: no pure-Python source to patch")
        spec.loader = _PatchedLoader(fullname, spec.origin)
        return spec


def install():
    """Install the finder; must run before any patched module is imported."""
    already = [name for name in PATCHES if name in sys.modules]
    if already:
        raise PatchError(f"patch targets imported before the hook: {', '.join(already)}")
    if not any(isinstance(finder, _PatchFinder) for finder in sys.meta_path):
        sys.meta_path.insert(0, _PatchFinder())


def module_source(fullname):
    """Locate a target's source without importing it (parents are imported)."""
    parent, _, _ = fullname.rpartition(".")
    package = importlib.import_module(parent)
    spec = importlib.machinery.PathFinder.find_spec(fullname, package.__path__)
    if spec is None or not spec.origin:
        raise PatchError(f"{fullname}: module not found")
    with open(spec.origin, "rb") as handle:
        return importlib.util.decode_source(handle.read())


def verify():
    """Apply every patch to the installed sources without importing them; returns patch labels."""
    for fullname in PATCHES:
        patched_source(fullname, module_source(fullname))
    return sorted(patch.__doc__.split(":", 1)[0] for patches in PATCHES.values() for patch in patches)
