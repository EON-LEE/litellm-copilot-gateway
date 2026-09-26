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

# Always pull the latest Copilot model catalog before launching, so new
# releases (e.g. a new GPT/Claude model on Copilot) show up without a manual
# refresh step. refresh-models.sh is fail-closed (won't touch config.yaml on
# a bad/short response), so a network hiccup here just falls back to the
# existing config instead of blocking the launch.
echo "🔄 Refreshing Copilot model catalog..." >&2
if "$DIR/refresh-models.sh" >&2; then
  "$DIR/restart-proxy.sh" >&2
else
  if [ ! -s "$DIR/config.yaml" ]; then
    echo "ERROR: model refresh failed and no existing config.yaml is available" >&2
    exit 1
  fi
  echo "⚠️  Model refresh failed — continuing with existing config.yaml" >&2
  # LiteLLM discovery can succeed while its Copilot backend is down.
  "$DIR/start-proxy.sh" >&2
fi

export ANTHROPIC_BASE_URL="http://127.0.0.1:4000"
export ANTHROPIC_AUTH_TOKEN="$LITELLM_MASTER_KEY"
export CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1

# Defaults — override by exporting before running, or switch live with /model.
export ANTHROPIC_MODEL="${ANTHROPIC_MODEL:-claude-sonnet-5}"
export ANTHROPIC_DEFAULT_OPUS_MODEL="${ANTHROPIC_DEFAULT_OPUS_MODEL:-claude-opus-5}"
export ANTHROPIC_DEFAULT_SONNET_MODEL="${ANTHROPIC_DEFAULT_SONNET_MODEL:-claude-sonnet-5}"
# A named model must call that actual Copilot model, never a substitute.
export ANTHROPIC_DEFAULT_HAIKU_MODEL="${ANTHROPIC_DEFAULT_HAIKU_MODEL:-claude-haiku-4.5}"
# Explicit helper model for older clients. copilot-api separately runs hosted
# WebSearch on its configured messageApiWebSearchModel, not on Haiku.
export ANTHROPIC_SMALL_FAST_MODEL="${ANTHROPIC_SMALL_FAST_MODEL:-gpt-5-mini}"

# Force the model unless the caller passed --model: a user's saved default model
# (e.g. claude-fable-5 from /model) would otherwise override ANTHROPIC_MODEL and
# request a model that doesn't exist behind the gateway.
for arg in "$@"; do
  case "$arg" in
    --) break ;;
    --model|--model=*) exec claude --dangerously-skip-permissions "$@" ;;
  esac
done
exec claude --dangerously-skip-permissions --model "$ANTHROPIC_MODEL" "$@"
