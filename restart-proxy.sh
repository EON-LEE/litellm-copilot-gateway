#!/usr/bin/env bash
# Restart only litellm (config reload). copilot-api keeps running.
set -euo pipefail
"$HOME/litellm-copilot-gateway/stop-proxy.sh" litellm
exec "$HOME/litellm-copilot-gateway/start-proxy.sh"
