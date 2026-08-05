#!/usr/bin/env bash
# Launch Claude Code backed by GitHub Copilot (via the local LiteLLM gateway).
# Your normal `claude` (real Anthropic) is unaffected — env vars are set only
# for this child process.
#
#   ~/litellm-copilot-gateway/claude-copilot.sh                    # default: claude-sonnet-5
#   ~/litellm-copilot-gateway/claude-copilot.sh --model gpt-5.5    # any Copilot model
#   inside a session: /model  (aliases claude-gpt-... are the same gpt models)
set -euo pipefail
DIR="$HOME/litellm-copilot-gateway"
# shellcheck disable=SC1091
source "$DIR/.env"

if ! curl -fsS --max-time 5 http://127.0.0.1:4000/v1/models \
     -H "Authorization: Bearer $LITELLM_MASTER_KEY" >/dev/null 2>&1; then
  echo "⚠️  Gateway not reachable on :4000 — starting it..." >&2
  "$DIR/start-proxy.sh" >&2
fi

export ANTHROPIC_BASE_URL="http://127.0.0.1:4000"
export ANTHROPIC_AUTH_TOKEN="$LITELLM_MASTER_KEY"
export CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1

# Defaults — override by exporting before running, or switch live with /model.
export ANTHROPIC_MODEL="${ANTHROPIC_MODEL:-claude-sonnet-5}"
export ANTHROPIC_DEFAULT_OPUS_MODEL="${ANTHROPIC_DEFAULT_OPUS_MODEL:-claude-opus-5}"
export ANTHROPIC_DEFAULT_SONNET_MODEL="${ANTHROPIC_DEFAULT_SONNET_MODEL:-claude-sonnet-5}"
# gpt-5-mini, not gpt-4o-mini: gpt-5-mini is in Copilot's current model catalog with
# vision registered (gpt-4o-mini images 400 upstream: "image media type not supported"),
# routes via copilot-api /responses (streams correctly), and is copilot-api's default
# messageApiWebSearchModel so WebSearch requests stay on the same model.
export ANTHROPIC_DEFAULT_HAIKU_MODEL="${ANTHROPIC_DEFAULT_HAIKU_MODEL:-gpt-5-mini}"
export ANTHROPIC_SMALL_FAST_MODEL="${ANTHROPIC_SMALL_FAST_MODEL:-gpt-5-mini}"   # older CC versions

# Force the model unless the caller passed --model: a user's saved default model
# (e.g. claude-fable-5 from /model) would otherwise override ANTHROPIC_MODEL and
# request a model that doesn't exist behind the gateway.
for arg in "$@"; do
  if [ "$arg" = "--model" ]; then exec claude --dangerously-skip-permissions "$@"; fi
done
exec claude --dangerously-skip-permissions --model "$ANTHROPIC_MODEL" "$@"
