#!/usr/bin/env bash
# Start the full Copilot gateway stack, localhost-only:
#   copilot-api :4141 (Anthropic->Copilot /responses translator, for gpt-5.x/codex/grok)
#   litellm     :4000 (single Anthropic-compatible front for Claude Code)
# Idempotent: skips anything already listening. Logs: copilot-api.log / proxy.log
set -euo pipefail
export PATH="$HOME/.local/bin:/opt/homebrew/bin:$PATH"
DIR="$HOME/litellm-copilot"
# shellcheck disable=SC1091
source "$DIR/.env"    # LITELLM_MASTER_KEY
export LITELLM_MASTER_KEY

listening() { lsof -iTCP:"$1" -sTCP:LISTEN -n -P >/dev/null 2>&1; }

if listening 4141; then
  echo "copilot-api  : already listening on 127.0.0.1:4141"
else
  # Token via copilot-api's own token store, NOT -g argv (argv is visible in `ps`).
  # Seed the store from litellm's long-lived OAuth token if missing.
  CAPI_TOKEN="$HOME/.local/share/copilot-api/github_token"
  if [ ! -s "$CAPI_TOKEN" ]; then
    mkdir -p "$(dirname "$CAPI_TOKEN")"
    (umask 077; cat "$HOME/.config/litellm/github_copilot/access-token" > "$CAPI_TOKEN")
  fi
  chmod 600 "$CAPI_TOKEN" 2>/dev/null || true
  HOST=127.0.0.1 nohup npx -y @jeffreycao/copilot-api@latest start --port 4141 \
    >> "$DIR/copilot-api.log" 2>&1 &
  for _ in $(seq 1 30); do listening 4141 && break; sleep 1; done
  listening 4141 && echo "copilot-api  : started on 127.0.0.1:4141" \
                 || { echo "copilot-api  : FAILED to start — see $DIR/copilot-api.log"; exit 1; }
fi

if listening 4000; then
  echo "litellm      : already listening on 127.0.0.1:4000"
else
  nohup litellm --config "$DIR/config.yaml" --host 127.0.0.1 --port 4000 \
    >> "$DIR/proxy.log" 2>&1 &
  for _ in $(seq 1 45); do listening 4000 && break; sleep 1; done
  listening 4000 && echo "litellm      : started on 127.0.0.1:4000" \
                 || { echo "litellm      : FAILED to start — see $DIR/proxy.log"; exit 1; }
fi

echo "health check :"
curl -fsS --max-time 15 http://127.0.0.1:4000/v1/models \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" | jq -r '.data | length | "  \(.) models exposed"' \
  || echo "  WARNING: /v1/models failed"
