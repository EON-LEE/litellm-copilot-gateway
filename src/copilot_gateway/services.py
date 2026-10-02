"""Cross-platform process management for the gateway services (psutil, no lsof/ps/curl).

Safety rules carried over from the bash scripts:
* a port is only ever signalled after every listener on it is verified (by
  command line) to be a gateway process; anything else is refused;
* POSIX gets SIGTERM only (no SIGKILL); Windows terminates verified PIDs only;
* nothing is killed by name.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from typing import Callable
import urllib.error
import urllib.request

import psutil

from .settings import LEGACY_REPO

WINDOWS = sys.platform == "win32"


class ServiceError(RuntimeError):
    pass


def _norm(value) -> str:
    return os.path.normcase(os.path.abspath(str(value))).replace("\\", "/")


@dataclass
class Service:
    name: str
    port: int
    log: Path
    kind: str  # "capi" | "litellm"
    config: Path | None = None
    command: Callable[[], list] | None = None
    env: dict = field(default_factory=dict)
    auth: str | None = None
    startup_timeout: float = 90

    def classify(self, cmdline) -> str | None:
        """'ours' when the command line is this gateway service, 'legacy' for the bash-era one."""
        args = [str(arg) for arg in cmdline or []]
        port = str(self.port)
        if self.kind == "capi":
            for index, arg in enumerate(args):
                if (arg.replace("\\", "/").endswith("@jeffreycao/copilot-api/dist/main.js")
                        and args[index + 1:index + 4] == ["start", "--port", port]):
                    return "ours"
            return None
        if "copilot_gateway.litellm_runner" in args:
            tail = args[args.index("copilot_gateway.litellm_runner") + 1:]
            if _flag(tail, "--port") == port and _flag(tail, "--config") and \
                    _norm(_flag(tail, "--config")) == _norm(self.config):
                return "ours"
            return None
        if (self.name == "claude" and _flag(args, "--port") == port and _flag(args, "--config")
                and any(Path(arg).name in ("litellm", "litellm.exe") for arg in args[:3])
                and _norm(_flag(args, "--config")) == _norm(LEGACY_REPO / "config.yaml")):
            return "legacy"
        return None


def _flag(args, name):
    for index, arg in enumerate(args[:-1]):
        if arg == name:
            return args[index + 1]
    return None


def port_open(port, host="127.0.0.1", timeout=0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def listener_pids(port) -> set:
    def listening(conn):
        return conn.status == psutil.CONN_LISTEN and conn.laddr and conn.laddr.port == port

    try:
        return {conn.pid for conn in psutil.net_connections(kind="tcp") if listening(conn)}
    except psutil.AccessDenied:  # macOS without root: inspect processes we can see
        pids = set()
        for proc in psutil.process_iter():
            try:
                if any(listening(conn) for conn in proc.net_connections(kind="tcp")):
                    pids.add(proc.pid)
            except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
                continue
        return pids


@dataclass
class State:
    status: str  # stopped | running | legacy | foreign
    processes: list
    detail: str = ""


def inspect(service) -> State:
    pids = listener_pids(service.port)
    if not pids:
        if port_open(service.port):
            return State("foreign", [], "listener owner not visible (another user, WSL relay, or container?)")
        return State("stopped", [])
    processes, kinds, details = [], set(), []
    for pid in pids:
        if pid is None:
            kinds.add(None)
            details.append("unknown pid")
            continue
        try:
            proc = psutil.Process(pid)
            kind = service.classify(proc.cmdline())
            details.append(f"{proc.name()} pid {pid}")
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        except psutil.AccessDenied:
            kind = None
            details.append(f"pid {pid} (access denied)")
        kinds.add(kind)
        processes.append(proc)
    if not processes and None not in kinds:
        return State("stopped", [])
    if None in kinds:
        return State("foreign", processes, ", ".join(details))
    return State("legacy" if "legacy" in kinds else "running", processes, ", ".join(details))


def _family(service, processes):
    """Add a verified same-command parent (Windows venv launcher re-execs python)."""
    family = list(processes)
    for proc in processes:
        try:
            parent = proc.parent()
            if parent and parent not in family and service.classify(parent.cmdline()):
                family.append(parent)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return family


def stop(service, log=print, timeout=20) -> bool:
    state = inspect(service)
    if state.status == "stopped":
        return False
    if state.status == "foreign":
        raise ServiceError(f"{service.name}: port {service.port} is held by a non-gateway process "
                           f"({state.detail}); refusing to signal it")
    family = _family(service, state.processes)
    for proc in family:
        try:
            proc.terminate()  # SIGTERM on POSIX; TerminateProcess on Windows (verified PIDs only)
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(family, timeout=timeout)
    if alive:
        raise ServiceError(f"{service.name}: still running after SIGTERM: "
                           + ", ".join(str(proc.pid) for proc in alive) + " (not force-killed)")
    deadline = time.monotonic() + 5
    while port_open(service.port) and time.monotonic() < deadline:
        time.sleep(0.2)
    log(f"{service.name:<8}: stopped (port {service.port})")
    return True


def _spawn(service):
    service.log.parent.mkdir(parents=True, exist_ok=True)
    command = service.command()
    env = {**os.environ, **service.env}
    with open(service.log, "ab") as log:
        kwargs = dict(stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, env=env,
                      cwd=str(service.log.parent))
        if not WINDOWS:
            return subprocess.Popen(command, start_new_session=True, **kwargs)
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        try:
            return subprocess.Popen(command, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **kwargs)
        except OSError:
            return subprocess.Popen(command, creationflags=flags, **kwargs)


def http_models(port, auth=None, timeout=15):
    headers = {"Authorization": f"Bearer {auth}"} if auth else {}
    request = urllib.request.Request(f"http://127.0.0.1:{port}/v1/models", headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8")).get("data")
    if not isinstance(data, list) or not data:
        raise ServiceError(f"no models exposed on port {port}")
    return data


def _log_tail(path, lines=15):
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


def start(service, log=print) -> bool:
    state = inspect(service)
    if state.status == "running":
        log(f"{service.name:<8}: already running on 127.0.0.1:{service.port}")
        return False
    if state.status == "legacy":
        raise ServiceError(f"{service.name}: a bash-era gateway holds port {service.port}; run `ccgw restart`")
    if state.status == "foreign":
        raise ServiceError(f"{service.name}: port {service.port} is in use by another process ({state.detail}). "
                           "Stop it or choose other ports (CCGW_PORT / CCGW_CODEX_PORT / CCGW_CAPI_PORT).")
    process = _spawn(service)
    deadline = time.monotonic() + service.startup_timeout
    last_error = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise ServiceError(f"{service.name}: exited with {process.returncode} during startup; "
                               f"see {service.log}\n{_log_tail(service.log)}")
        if port_open(service.port):
            try:
                http_models(service.port, service.auth)
                log(f"{service.name:<8}: started on 127.0.0.1:{service.port} (pid {process.pid})")
                return True
            except (OSError, ValueError, ServiceError, urllib.error.URLError) as error:
                last_error = error
        time.sleep(0.5)
    raise ServiceError(f"{service.name}: not ready after {service.startup_timeout:.0f}s ({last_error}); "
                       f"see {service.log}\n{_log_tail(service.log)}")


def check_ready(service):
    try:
        return len(http_models(service.port, service.auth))
    except (OSError, ValueError, urllib.error.URLError) as error:
        raise ServiceError(f"{service.name}: /v1/models readiness failed on port {service.port}: {error}") from None
