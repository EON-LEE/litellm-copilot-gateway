#!/usr/bin/env bash
# Deprecated: kept so existing aliases keep working. Patches now apply in memory at import; doctor verifies them.
set -euo pipefail
if ! command -v ccgw >/dev/null 2>&1; then
  echo "ccgw not found. Install: uv tool install --python 3.13 git+https://github.com/EON-LEE/litellm-copilot-gateway" >&2
  exit 127
fi
exec ccgw doctor "$@"
