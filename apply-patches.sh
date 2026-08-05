#!/usr/bin/env bash
# Re-apply local patches to the litellm install.
# RUN THIS AFTER EVERY `uv tool install/upgrade litellm` — upgrades wipe patches.
#
# Patch 1: github_copilot authenticator device-login window 1min -> 10min
#          (upstream polls a device code for only 12*5s before rotating it,
#           which races the human typing the code into github.com/login/device)
set -euo pipefail

SITE="$HOME/.local/share/uv/tools/litellm/lib/python3.13/site-packages"
AUTH="$SITE/litellm/llms/github_copilot/authenticator.py"

if [ ! -f "$AUTH" ]; then
  echo "ERROR: $AUTH not found (litellm not installed via uv tool?)" >&2
  exit 1
fi

if grep -q "max_attempts = 120" "$AUTH"; then
  echo "Patch 1 already applied."
elif grep -q "max_attempts = 12 " "$AUTH" || grep -q "max_attempts = 12$" "$AUTH" || grep -qE "max_attempts = 12\b" "$AUTH"; then
  sed -i '' -E 's/max_attempts = 12[[:space:]]*#.*/max_attempts = 120  # PATCHED: 10 minutes (was 12 = 1 minute)/' "$AUTH"
  grep -q "max_attempts = 120" "$AUTH" && echo "Patch 1 applied (device-login window -> 10min)." || { echo "ERROR: patch 1 failed"; exit 1; }
else
  echo "WARNING: expected 'max_attempts = 12' not found — upstream may have changed; inspect $AUTH manually." >&2
fi
