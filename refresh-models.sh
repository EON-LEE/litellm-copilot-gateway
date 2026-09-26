#!/usr/bin/env bash
# Regenerate config.yaml from Copilot's user-visible catalog.
# One public name per upstream model; hidden aliases only for that SAME model.
# Native Claude and Responses models use copilot-api; chat-only uses LiteLLM direct.
# model_catalog.py owns filtering, routing, labels, and catalog token limits.
set -euo pipefail

DIR="$HOME/litellm-copilot-gateway"
CFG="$DIR/config.yaml"
CRED="$HOME/.config/litellm/github_copilot"

# Mint a short-lived bearer without printing credentials or writing them to config.
GH_TOKEN=$(cat "$CRED/access-token")
BEARER=$(curl -fsS --max-time 20 https://api.github.com/copilot_internal/v2/token \
  -H "Authorization: token $GH_TOKEN" \
  -H "editor-version: vscode/1.100.0" | jq -re '.token')
MODELS_JSON=$(curl -fsS --max-time 20 https://api.githubcopilot.com/models \
  -H "Authorization: Bearer $BEARER" \
  -H "Copilot-Integration-Id: vscode-chat" \
  -H "editor-version: vscode/1.100.0")

# Fail-closed: build and validate everything before replacing a working config.
TMP=$(mktemp "$DIR/.config.yaml.XXXXXX")
trap 'rm -f "$TMP"' EXIT
python3 "$DIR/model_catalog.py" <<<"$MODELS_JSON" > "$TMP"
if [ -f "$CFG" ] && cmp -s "$TMP" "$CFG"; then
  echo "Copilot model catalog unchanged."
  exit 0
fi
[ -f "$CFG" ] && cp "$CFG" "$CFG.bak"
mv "$TMP" "$CFG"
trap - EXIT
COUNT=$(python3 -c 'import json,sys; print(len(json.loads(sys.stdin.read().split("\n",1)[1])["model_list"]))' < "$CFG")
echo "Wrote $CFG ($COUNT visible Copilot models; one entry per model)."
echo "Apply with: $DIR/restart-proxy.sh"
