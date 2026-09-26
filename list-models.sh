#!/usr/bin/env bash
# List user-visible Copilot models with real names and separate token limits.
# Uses the SAME selection policy as refresh-models.sh; no aliases or legacy extras.
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
| python3 "$HOME/litellm-copilot-gateway/model_catalog.py" --list | column -t -s $'\t'
