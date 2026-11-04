"""ccp — launch Claude Code against the Copilot gateway.

Your normal `claude` (real Anthropic) is unaffected: the gateway env vars are
set only for this child process.

    ccp                       # default: the newest Sonnet your Copilot account offers
    ccp --model claude-gpt-6.1-sol
    ccp --no-refresh ...      # skip the catalog refresh (faster start)
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

from .. import catalog, gateway, services
from ..settings import Settings

FAMILY_VARS = (("ANTHROPIC_DEFAULT_OPUS_MODEL", "opus"), ("ANTHROPIC_DEFAULT_SONNET_MODEL", "sonnet"),
               ("ANTHROPIC_DEFAULT_HAIKU_MODEL", "haiku"), ("ANTHROPIC_DEFAULT_FABLE_MODEL", "fable"))
MODEL_VARS = ("ANTHROPIC_MODEL", *(name for name, _ in FAMILY_VARS), "ANTHROPIC_SMALL_FAST_MODEL")
# Claude Code resolves these itself through the ANTHROPIC_DEFAULT_*_MODEL slots.
CLIENT_ALIASES = {"default", "best", "fable", "opus", "sonnet", "haiku", "opusplan",
                  "fable[1m]", "opus[1m]", "sonnet[1m]"}


def load_config(settings):
    return catalog.parse_rendered(settings.claude_config.read_text(encoding="utf-8"))


def model_defaults(config):
    """Defaults from the live catalog. A family the account lacks stays unset: never a substitute model."""
    families = catalog.claude_families(config)
    defaults = {name: families[family] for name, family in FAMILY_VARS if family in families}
    main = next((families[family] for family in ("sonnet", "opus", "haiku") if family in families), None)
    defaults["ANTHROPIC_MODEL"] = main or config["model_list"][0]["model_name"]
    if "haiku" in families:
        # Deprecated Haiku-slot variable that older Claude Code versions still read.
        defaults["ANTHROPIC_SMALL_FAST_MODEL"] = families["haiku"]
    return defaults


def claude_env(settings, key, base=None, config=None):
    env = dict(os.environ if base is None else base)
    env.update({
        "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{settings.claude_port}",
        "ANTHROPIC_AUTH_TOKEN": key,
        "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1",
    })
    for name, value in model_defaults(load_config(settings) if config is None else config).items():
        env.setdefault(name, value)
    return env


def unavailable_models(env, config):
    """Model variables naming something this gateway does not serve (Claude Code would get errors)."""
    served = {row["model_name"] for row in config["model_list"]} | set(config["router_settings"]["model_group_alias"])
    return [(name, env[name]) for name in MODEL_VARS
            if env.get(name) and env[name] not in served and env[name].lower() not in CLIENT_ALIASES]


def describe(env):
    slots = [f"{family}: {env.get(name) or 'not offered'}" for name, family in FAMILY_VARS
             if family != "fable" or env.get(name)]
    return f"Models: {env['ANTHROPIC_MODEL']} · " + " · ".join(slots)


def claude_args(argv, model):
    """Force the gateway default unless the caller chose --model (a saved /model may not exist here)."""
    for arg in argv:
        if arg == "--":
            break
        if arg == "--model" or arg.startswith("--model="):
            return ["--dangerously-skip-permissions", *argv]
    return ["--dangerously-skip-permissions", "--model", model, *argv]


def run(command, env):
    try:
        if os.name == "posix":
            os.execvpe(command[0], command, env)
        # Windows has no exec: keep Ctrl+C for the child, then mirror its exit code.
        import signal
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        return subprocess.call(command, env=env)
    except OSError as error:
        gateway.eprint(f"ERROR: cannot run {command[0]}: {error}")
        return 127


def main(argv=None):
    gateway.safe_console()
    argv = list(sys.argv[1:] if argv is None else argv)
    if gateway.is_help(argv):
        gateway.eprint(__doc__)
        executable = os.environ.get("CCP_CLAUDE") or shutil.which("claude")
        return run([executable, *argv], dict(os.environ)) if executable else 0
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
    config = load_config(settings)
    env = claude_env(settings, settings.master_key(), config=config)
    gateway.eprint(describe(env))
    for name, value in unavailable_models(env, config):
        gateway.eprint(f"⚠️  {name}={value} is not offered by this Copilot account; requests for it will fail "
                       "(`ccgw models` lists the offered models)")
    return run([executable, *claude_args(argv, env["ANTHROPIC_MODEL"])], env)


if __name__ == "__main__":
    sys.exit(main())
