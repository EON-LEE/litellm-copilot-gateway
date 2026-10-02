"""ccp — launch Claude Code against the Copilot gateway.

Your normal `claude` (real Anthropic) is unaffected: the gateway env vars are
set only for this child process.

    ccp                       # default model claude-sonnet-5
    ccp --model claude-gpt-5.5
    ccp --no-refresh ...      # skip the catalog refresh (faster start)
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

from .. import gateway, services
from ..settings import Settings

DEFAULTS = {
    "ANTHROPIC_MODEL": "claude-sonnet-5",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus-5",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-sonnet-5",
    # A named model must call that actual Copilot model, never a substitute.
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-haiku-4.5",
    # Explicit helper model for older clients; hosted WebSearch runs on copilot-api's own model.
    "ANTHROPIC_SMALL_FAST_MODEL": "gpt-5-mini",
}


def claude_env(settings, key, base=None):
    env = dict(os.environ if base is None else base)
    env.update({
        "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{settings.claude_port}",
        "ANTHROPIC_AUTH_TOKEN": key,
        "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1",
    })
    for name, value in DEFAULTS.items():
        env.setdefault(name, value)
    return env


def claude_args(argv, model):
    """Force the gateway default unless the caller chose --model (a saved /model may not exist here)."""
    for arg in argv:
        if arg == "--":
            break
        if arg == "--model" or arg.startswith("--model="):
            return ["--dangerously-skip-permissions", *argv]
    return ["--dangerously-skip-permissions", "--model", model, *argv]


def run(command, env):
    if os.name == "posix":
        os.execvpe(command[0], command, env)
    # Windows has no exec: keep Ctrl+C for the child, then mirror its exit code.
    import signal
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    return subprocess.call(command, env=env)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    do_refresh = True
    if argv[:1] == ["--no-refresh"]:
        do_refresh, argv = False, argv[1:]
    settings = Settings.load()
    try:
        gateway.ensure(settings, ["claude"], do_refresh=do_refresh)
    except (services.ServiceError, RuntimeError) as error:
        gateway.eprint(f"ERROR: {error}")
        return 1
    executable = os.environ.get("CCP_CLAUDE") or shutil.which("claude")
    if not executable:
        gateway.eprint("ERROR: Claude Code (`claude`) not found on PATH; install it first")
        return 1
    env = claude_env(settings, settings.master_key())
    return run([executable, *claude_args(argv, env["ANTHROPIC_MODEL"])], env)


if __name__ == "__main__":
    sys.exit(main())
