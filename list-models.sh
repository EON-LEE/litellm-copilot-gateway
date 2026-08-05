#!/usr/bin/env bash
# List all chat models GitHub Copilot currently exposes (id, endpoints, context window).
set -euo pipefail
CRED="$HOME/.config/litellm/github_copilot"
GH_TOKEN=$(cat "$CRED/access-token")
BEARER=$(curl -fsS --max-time 20 https://api.github.com/copilot_internal/v2/token \
  -H "Authorization: token $GH_TOKEN" \
  -H "editor-version: vscode/1.100.0" | jq -re '.token')
curl -fsS --max-time 20 https://api.githubcopilot.com/models \
  -H "Authorization: Bearer $BEARER" \
  -H "Copilot-Integration-Id: vscode-chat" \
  -H "editor-version: vscode/1.100.0" \
| jq -r '.data[] | select((.capabilities.type // "")=="chat")
         | [.id, ((.supported_endpoints // ["chat-only"]) | join(" ")), "ctx=\(.capabilities.limits.max_prompt_tokens // "?")"]
         | @tsv' | sort | column -t -s $'\t'
