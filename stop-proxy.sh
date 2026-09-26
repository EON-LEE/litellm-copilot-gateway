#!/usr/bin/env bash
# Stop only verified gateway listener PIDs, never processes selected by name.
set -euo pipefail
export PATH="$HOME/.local/bin:/opt/homebrew/bin:$PATH"
if [ "$#" -gt 1 ]; then
  echo "Usage: $0 [all|litellm|copilot-api]" >&2
  exit 1
fi
python3 - "$HOME/litellm-copilot-gateway" "${1:-all}" <<'PYEOF'
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import time

root = Path(sys.argv[1]).resolve()
ports = {"litellm": 4000, "copilot-api": 4141}


def listeners(port):
    result = subprocess.run(
        ["lsof", "-t", "-a", f"-iTCP:{port}", "-sTCP:LISTEN", "-n", "-P"],
        text=True, capture_output=True, check=False,
    )
    if result.returncode not in (0, 1) or result.stderr.strip():
        raise RuntimeError(f"cannot inspect listeners on :{port}")
    pids = {int(pid) for pid in result.stdout.split()}
    if any(pid <= 0 for pid in pids):
        raise RuntimeError(f"invalid listener PID on :{port}; no signal sent")
    return pids


def verify_process(pid, service):
    result = subprocess.run(["ps", "-p", str(pid), "-o", "args="],
                            text=True, capture_output=True, check=False)
    if result.returncode == 1 and not result.stdout.strip():
        return False
    if result.returncode != 0:
        raise RuntimeError(f"cannot inspect PID {pid}; no signal sent")
    args = shlex.split(result.stdout)
    matches = False
    if service == "litellm" and len(args) >= 7:
        executable = shutil.which("litellm")
        matches = (
            executable is not None
            and Path(args[-7]).resolve() == Path(executable).resolve()
            and args[-6:] == ["--config", str(root / "config.yaml"),
                             "--host", "127.0.0.1", "--port", "4000"]
        )
    elif service == "copilot-api" and len(args) == 5 and Path(args[0]).name in ("node", "nodejs"):
        entry = Path(args[1]).resolve()
        package = entry.parent.parent
        manifest = package / "package.json"
        if (entry == package / "dist/main.js" and manifest.is_file()
                and package.is_relative_to(Path.home() / ".npm")
                and args[2:] == ["start", "--port", "4141"]):
            info = json.loads(manifest.read_text())
            matches = (isinstance(info, dict) and info.get("name") == "@jeffreycao/copilot-api"
                       and isinstance(info.get("bin"), dict)
                       and info["bin"].get("copilot-api") == "./dist/main.js")
    if not matches:
        raise RuntimeError(f"PID {pid} on :{ports[service]} is not this gateway's {service}; no signal sent")
    return True


def main():
    scope = sys.argv[2]
    if scope not in ("all", *ports):
        raise RuntimeError("expected all, litellm, or copilot-api")
    services = list(ports) if scope == "all" else [scope]
    targets = {}
    # Validate every target before stopping any service.
    for service in services:
        targets[service] = {pid for pid in listeners(ports[service]) if verify_process(pid, service)}
    for service, pids in targets.items():
        if not pids:
            print(f"{service} not running")
            continue
        for pid in sorted(pids):
            if pid not in listeners(ports[service]) or not verify_process(pid, service):
                continue
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + 20
        while listeners(ports[service]):
            if time.monotonic() >= deadline:
                raise RuntimeError(f":{ports[service]} still occupied after 20s; not forcing termination")
            time.sleep(0.2)
        print(f"{service} stopped")


try:
    main()
except (OSError, ValueError, RuntimeError) as error:
    print(f"ERROR: {error}", file=sys.stderr)
    sys.exit(1)
PYEOF
