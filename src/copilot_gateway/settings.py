"""OS-neutral locations, ports, and the gateway master key.

Everything the gateway writes lives in one per-user data directory, so the
same code behaves identically on Windows, macOS, Linux, and WSL. Override it
with CCGW_HOME. Values in ``<data>/.env`` act as defaults for the CCGW_* and
LITELLM_MASTER_KEY settings; the process environment wins for CCGW_* values.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import secrets
import sys

APP = "copilot-gateway"
LEGACY_REPO = Path.home() / "litellm-copilot-gateway"
DEFAULT_PORTS = {"CCGW_PORT": 4000, "CCGW_CODEX_PORT": 4001, "CCGW_CAPI_PORT": 4141}


def data_dir(environ=None) -> Path:
    environ = os.environ if environ is None else environ
    if environ.get("CCGW_HOME"):
        return Path(environ["CCGW_HOME"]).expanduser()
    if sys.platform == "win32":
        base = Path(environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / APP


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


def write_private(path: Path, text: str) -> None:
    """Create/replace a file readable only by the current user (POSIX modes)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class Settings:
    home: Path
    claude_port: int
    codex_port: int
    capi_port: int
    token_dir: Path
    file_values: dict

    @classmethod
    def load(cls, environ=None) -> "Settings":
        environ = os.environ if environ is None else environ
        home = data_dir(environ)
        file_values = read_env_file(home / ".env")

        def port(name):
            raw = environ.get(name) or file_values.get(name) or DEFAULT_PORTS[name]
            try:
                value = int(raw)
            except ValueError:
                raise SystemExit(f"ERROR: {name} must be a TCP port number") from None
            if not 0 < value < 65536:
                raise SystemExit(f"ERROR: {name} must be a TCP port number")
            return value

        token_dir = Path(environ.get("GITHUB_COPILOT_TOKEN_DIR")
                         or Path.home() / ".config" / "litellm" / "github_copilot").expanduser()
        settings = cls(home, port("CCGW_PORT"), port("CCGW_CODEX_PORT"), port("CCGW_CAPI_PORT"),
                       token_dir, file_values)
        ports = (settings.claude_port, settings.codex_port, settings.capi_port)
        if len(set(ports)) != 3:
            raise SystemExit("ERROR: CCGW_PORT, CCGW_CODEX_PORT and CCGW_CAPI_PORT must differ")
        return settings

    @property
    def env_file(self) -> Path:
        return self.home / ".env"

    @property
    def claude_config(self) -> Path:
        return self.home / "config.yaml"

    @property
    def codex_config(self) -> Path:
        return self.home / "config-codex.yaml"

    @property
    def logs(self) -> Path:
        return self.home / "logs"

    @property
    def run(self) -> Path:
        return self.home / "run"

    @property
    def codex_home(self) -> Path:
        return self.home / "codex"

    @property
    def capi_base(self) -> str:
        return f"http://127.0.0.1:{self.capi_port}"

    def master_key(self, create: bool = True) -> str:
        key = self.file_values.get("LITELLM_MASTER_KEY")
        if key:
            return key
        if not create:
            raise SystemExit(f"ERROR: no LITELLM_MASTER_KEY in {self.env_file}; run `ccgw setup`")
        # Keep an existing bash-era key so already-configured clients keep working.
        key = read_env_file(LEGACY_REPO / ".env").get("LITELLM_MASTER_KEY") or "sk-" + secrets.token_urlsafe(32)
        existing = ""
        if self.env_file.exists():
            existing = self.env_file.read_text(encoding="utf-8")
            if existing and not existing.endswith("\n"):
                existing += "\n"
        write_private(self.env_file, existing + f"LITELLM_MASTER_KEY={key}\n")
        self.file_values["LITELLM_MASTER_KEY"] = key
        return key
