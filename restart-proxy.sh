#!/usr/bin/env bash
# Restart only litellm (config reload). copilot-api keeps running.
set -euo pipefail
pkill -f "litellm --config $HOME/litellm-copilot/config.yaml" 2>/dev/null || true
# Wait for the old process to actually release the port (pkill returns at signal
# delivery, not process exit — a fixed sleep races graceful shutdown).
for _ in $(seq 1 20); do
  lsof -iTCP:4000 -sTCP:LISTEN -n -P >/dev/null 2>&1 || break
  sleep 1
done
if lsof -iTCP:4000 -sTCP:LISTEN -n -P >/dev/null 2>&1; then
  echo "ERROR: old litellm still holding :4000 after 20s — not restarting" >&2
  exit 1
fi
exec "$HOME/litellm-copilot/start-proxy.sh"
