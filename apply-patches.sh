#!/usr/bin/env bash
# Re-apply local patches to the litellm install.
# RUN THIS AFTER EVERY `uv tool install/upgrade litellm` — upgrades wipe patches.
#
# Patch 1: github_copilot authenticator device-login window 1min -> 10min
#          (upstream polls a device code for only 12*5s before rotating it,
#           which races the human typing the code into github.com/login/device)
# Patch 2: drop a dangling forced tool_choice when Anthropic->OpenAI tool
#          translation leaves `tools` empty (e.g. Claude Code's WebSearch,
#          whose only tool is web_search_20250305 and gets bucketed away from
#          `regular_tools`). Without this, github_copilot/OpenAI-compatible
#          backends 400 with "tools are required when tool choice is
#          specified". Defense-in-depth alongside websearch_interception in
#          config.yaml, which is the primary fix (real search, not just no-crash).
# Patch 3: guard empty chunk.choices in the Anthropic SSE adapter
#          (_should_start_new_content_block). Copilot's /chat/completions
#          sends a final usage-only chunk with choices=[], which crashes
#          litellm's github_copilot-direct streaming mid-stream:
#          IndexError at streaming_iterator.py:892 — the stream dies after
#          content_block_start with a 500 error frame and no message_stop.
#          Affects every github_copilot-direct chat model (gpt-4.1, gpt-4o,
#          gemini-*, ...) when streamed via /v1/messages.
# Patch 4: expose allowlisted Copilot model_info in discovery responses, without
#          leaking configuration or changing discovery for other providers.
set -euo pipefail

SITE="$HOME/.local/share/uv/tools/litellm/lib/python3.13/site-packages"
AUTH="$SITE/litellm/llms/github_copilot/authenticator.py"
ADAPTER="$SITE/litellm/llms/anthropic/experimental_pass_through/adapters/transformation.py"
STREAMER="$SITE/litellm/llms/anthropic/experimental_pass_through/adapters/streaming_iterator.py"

if [ ! -f "$AUTH" ]; then
  echo "ERROR: $AUTH not found (litellm not installed via uv tool?)" >&2
  exit 1
fi

if grep -q "max_attempts = 120" "$AUTH"; then
  echo "Patch 1 already applied."
elif grep -q "max_attempts = 12 " "$AUTH" || grep -q "max_attempts = 12$" "$AUTH" || grep -qE "max_attempts = 12\b" "$AUTH"; then
  if sed --version >/dev/null 2>&1; then SEDI=(sed -i); else SEDI=(sed -i ''); fi   # GNU vs BSD
  "${SEDI[@]}" -E 's/max_attempts = 12[[:space:]]*#.*/max_attempts = 120  # PATCHED: 10 minutes (was 12 = 1 minute)/' "$AUTH"
  grep -q "max_attempts = 120" "$AUTH" && echo "Patch 1 applied (device-login window -> 10min)." || { echo "ERROR: patch 1 failed"; exit 1; }
else
  echo "WARNING: expected 'max_attempts = 12' not found — upstream may have changed; inspect $AUTH manually." >&2
fi

if [ ! -f "$ADAPTER" ]; then
  echo "ERROR: $ADAPTER not found (litellm not installed via uv tool?)" >&2
  exit 1
fi

if grep -q "PATCHED (local): drop dangling forced tool_choice" "$ADAPTER"; then
  echo "Patch 2 already applied."
else
  python3 - "$ADAPTER" <<'PYEOF'
import sys
path = sys.argv[1]
old = """        if not regular_tools:
            return {}
"""
new = """        if not regular_tools:
            new_kwargs.pop("tool_choice", None)  # PATCHED (local): drop dangling forced tool_choice
            return {}
"""
text = open(path).read()
count = text.count(old)
if count != 1:
    print(f"ERROR: expected exactly 1 match of anchor block, found {count} — upstream may have changed; inspect {path} manually.", file=sys.stderr)
    sys.exit(1)
open(path, "w").write(text.replace(old, new))
PYEOF
  grep -q "PATCHED (local): drop dangling forced tool_choice" "$ADAPTER" && echo "Patch 2 applied (drop dangling tool_choice when tools translate to empty)." || { echo "ERROR: patch 2 failed"; exit 1; }
fi

if [ ! -f "$STREAMER" ]; then
  echo "ERROR: $STREAMER not found (litellm not installed via uv tool?)" >&2
  exit 1
fi

if grep -q "PATCHED (local): guard empty choices" "$STREAMER"; then
  echo "Patch 3a already applied."
else
  python3 - "$STREAMER" <<'PYEOF'
import sys
path = sys.argv[1]
old = """        if chunk.choices[0].finish_reason is not None:
            return False
"""
new = """        if not chunk.choices:  # PATCHED (local): guard empty choices (Copilot's final usage-only chunk)
            return False
        if chunk.choices[0].finish_reason is not None:
            return False
"""
text = open(path).read()
count = text.count(old)
if count != 1:
    print(f"ERROR: expected exactly 1 match of anchor block, found {count} — upstream may have changed; inspect {path} manually.", file=sys.stderr)
    sys.exit(1)
open(path, "w").write(text.replace(old, new))
PYEOF
  grep -q "PATCHED (local): guard empty choices" "$STREAMER" && echo "Patch 3a applied (guard in _should_start_new_content_block)." || { echo "ERROR: patch 3a failed"; exit 1; }
fi

if grep -q "PATCHED (local): usage-only chunk" "$STREAMER"; then
  echo "Patch 3b already applied."
else
  python3 - "$STREAMER" <<'PYEOF'
import sys
path = sys.argv[1]
old = '''                if chunk == "None" or chunk is None:
                    raise Exception
'''
new = '''                if chunk == "None" or chunk is None:
                    raise Exception

                # PATCHED (local): usage-only chunk with empty choices (Copilot's
                # final /chat/completions chunk). Everything below dereferences
                # chunk.choices[0]; merge its usage into the held message_delta
                # if one is pending, otherwise drop it.
                if not getattr(chunk, "choices", None):
                    if self.holding_stop_reason_chunk is not None and getattr(chunk, "usage", None) is not None:
                        merged_chunk = self._merge_usage_into_held_stop_reason_chunk(chunk)
                        self.chunk_queue.append(merged_chunk)
                        self.queued_usage_chunk = True
                        self.holding_stop_reason_chunk = None
                        return self.chunk_queue.popleft()
                    continue
'''
text = open(path).read()
count = text.count(old)
if count != 2:
    print(f"ERROR: expected exactly 2 matches (sync __next__ + async __anext__), found {count} — upstream may have changed; inspect {path} manually.", file=sys.stderr)
    sys.exit(1)
open(path, "w").write(text.replace(old, new))
PYEOF
  grep -q "PATCHED (local): usage-only chunk" "$STREAMER" && echo "Patch 3b applied (loop-top empty-choices guard in __next__/__anext__)." || { echo "ERROR: patch 3b failed"; exit 1; }
fi

UTILS="$SITE/litellm/proxy/utils.py"
if [ ! -f "$UTILS" ]; then
  echo "ERROR: Patch 4: $UTILS not found (litellm not installed via uv tool?)" >&2
  exit 1
fi

python3 - "$UTILS" <<'PYEOF'
import ast
import sys
from pathlib import Path

path = Path(sys.argv[1])
marker = "# PATCHED (local): expose configured Copilot discovery metadata"
old = """    if not include_metadata:
        return base
"""
new = """    # PATCHED (local): expose configured Copilot discovery metadata
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

""" + old


def fail(reason):
    print(f"ERROR: Patch 4: {reason} — upstream may have changed; inspect {path} manually.", file=sys.stderr)
    sys.exit(1)


try:
    text = path.read_text()
    tree = ast.parse(text)
except (OSError, SyntaxError) as error:
    fail(f"cannot read/parse discovery source: {error}")

functions = [node for node in tree.body
             if isinstance(node, ast.FunctionDef) and node.name == "create_model_info_response"]
if len(functions) != 1:
    fail(f"expected exactly 1 create_model_info_response function, found {len(functions)}")

function = functions[0]
lines = text.splitlines(keepends=True)
start, end = function.lineno - 1, function.end_lineno
block = "".join(lines[start:end])
if block.count(new) == 1 and text.count(marker) == 1 and block.count(old) == 1:
    print("Patch 4 already applied.")
    sys.exit(0)
if marker in text:
    fail("existing discovery patch differs from the expected block")
if block.count(old) != 1:
    fail(f"expected exactly 1 discovery return anchor, found {block.count(old)}")

updated = "".join(lines[:start]) + block.replace(old, new) + "".join(lines[end:])
ast.parse(updated)
path.write_text(updated)
print("Patch 4 applied (configured Copilot discovery metadata).")
PYEOF
