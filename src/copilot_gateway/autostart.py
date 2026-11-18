"""Keep the gateway up without a terminal (Windows): a per-user scheduled task runs
`ccgw start --all --no-refresh` every few minutes. Already-running services are left alone,
so the task only (re)starts what is down — after a reboot, logoff or a killed session.

Processes started from a terminal, IDE or agent session inherit its Windows job object and die
when that host closes it, even with CREATE_BREAKAWAY_FROM_JOB. While the task exists, `ccgw`,
`ccp` and `ccx` therefore ask Task Scheduler to start services instead of spawning them."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from xml.sax.saxutils import escape

from . import services
from .services import WINDOWS, ServiceError

TASK = "copilot-gateway-autostart"
EVERY_MINUTES = 5
OVERRIDE_VARS = ("CCGW_HOME", "CCGW_PORT", "CCGW_CODEX_PORT", "CCGW_CAPI_PORT", "GITHUB_COPILOT_TOKEN_DIR")


def task_command() -> tuple[str, str]:
    python = Path(sys.executable)
    windowless = python.with_name("pythonw.exe")
    if windowless.exists():
        python = windowless
    return str(python), "-m copilot_gateway.autostart"


def task_xml() -> str:
    command, arguments = (escape(part) for part in task_command())
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>litellm-copilot-gateway: start any stopped gateway service</Description></RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <StartBoundary>2000-01-01T00:00:00</StartBoundary>
      <Repetition><Interval>PT{EVERY_MINUTES}M</Interval></Repetition>
      <Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Principals><Principal id="Author"><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT1H</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author"><Exec><Command>{command}</Command><Arguments>{arguments}</Arguments></Exec></Actions>
</Task>
"""


def _schtasks(*args):
    flags = subprocess.CREATE_NO_WINDOW if WINDOWS else 0
    return subprocess.run(["schtasks", *args], capture_output=True, text=True, errors="replace",
                          creationflags=flags)


def _require_windows():
    if not WINDOWS:
        raise ServiceError("ccgw autostart is Windows-only; on macOS/Linux run `ccgw start --all` "
                           "from a launchd agent or systemd user unit")


def enable() -> str:
    _require_windows()
    with tempfile.TemporaryDirectory() as folder:
        xml = Path(folder) / "task.xml"
        xml.write_text(task_xml(), encoding="utf-16")
        result = _schtasks("/Create", "/F", "/TN", TASK, "/XML", str(xml))
    if result.returncode != 0:
        raise ServiceError(f"cannot create scheduled task {TASK}: {(result.stderr or result.stdout).strip()}")
    run()
    return TASK


def disable() -> bool:
    _require_windows()
    if not is_enabled():
        return False
    result = _schtasks("/Delete", "/F", "/TN", TASK)
    if result.returncode != 0:
        raise ServiceError(f"cannot delete scheduled task {TASK}: {(result.stderr or result.stdout).strip()}")
    return True


def is_enabled() -> bool:
    return WINDOWS and _schtasks("/Query", "/TN", TASK).returncode == 0


def run() -> bool:
    return _schtasks("/Run", "/TN", TASK).returncode == 0


def delegate() -> bool:
    """Ask the scheduled task to start the stopped services; False means spawn directly."""
    if services.DIRECT_SPAWN:
        return False
    return is_enabled() and run()


def serves(settings) -> bool:
    """True when the task (plain user environment) would manage exactly these services.

    A caller that overrides the data folder, ports or token folder (a dev instance, tests)
    always spawns its own processes."""
    from .settings import Settings
    plain = {key: value for key, value in os.environ.items() if key not in OVERRIDE_VARS}
    task_view = Settings.load(plain)
    return WINDOWS and all(getattr(task_view, name) == getattr(settings, name)
                           for name in ("home", "claude_port", "codex_port", "capi_port", "token_dir"))


def main():
    services.DIRECT_SPAWN = True
    from . import cli
    from .settings import Settings
    log_path = Settings.load().logs / "autostart.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    mode = "w" if log_path.exists() and log_path.stat().st_size > 1_000_000 else "a"
    with open(log_path, mode, encoding="utf-8", errors="replace") as log:
        sys.stdout = sys.stderr = log  # pythonw has no console
        print(time.strftime("--- %Y-%m-%d %H:%M:%S"), flush=True)
        return cli.main(["start", "--all", "--no-refresh"])


if __name__ == "__main__":
    sys.exit(main())
