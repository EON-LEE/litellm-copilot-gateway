#!/usr/bin/env bash
# Deprecated: kept so existing aliases keep working. Use ccp.
set -euo pipefail
if ! command -v ccp >/dev/null 2>&1; then
  echo "ccp not found. Install: uv tool install --python 3.13 git+https://github.com/EON-LEE/litellm-copilot-gateway" >&2
  exit 127
fi
exec ccp "$@"
