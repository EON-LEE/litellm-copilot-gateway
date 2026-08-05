#!/usr/bin/env bash
# Stop both gateway services.
pkill -f "litellm --config $HOME/litellm-copilot-gateway/config.yaml" 2>/dev/null && echo "litellm stopped" || echo "litellm not running"
pkill -f "copilot-api.*start" 2>/dev/null && echo "copilot-api stopped" || echo "copilot-api not running"
