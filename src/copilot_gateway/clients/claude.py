"""ccp — launch Claude Code against the Copilot gateway.

Your normal `claude` (real Anthropic) is unaffected: the gateway env vars are
set only for this child process.

    ccp                       # default: the newest Sonnet your Copilot account offers
    ccp --model claude-gpt-6.1-sol
    ccp --no-refresh ...      # skip the catalog refresh (faster start)
    ccp --respect-permissions ...  # let the caller control Claude's permissions
    ccp --sync-model-picker ...   # show only this account's models
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

from .. import catalog, gateway, services
from ..settings import Settings
from . import model_picker

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


def claude_args(argv, model, *, skip_permissions=True):
    """Force the gateway default unless the caller chose --model (a saved /model may not exist here)."""
    prefix = ["--dangerously-skip-permissions"] if skip_permissions else []
    for arg in argv:
        if arg == "--":
            break
        if arg == "--model" or arg.startswith("--model="):
            return [*prefix, *argv]
    return [*prefix, "--model", model, *argv]


def picker_model(value, env, config):
    """Resolve only aliases which name an offered model; never substitute another family."""
    requested = value
    family = None
    aliases = config["router_settings"]["model_group_alias"]
    served = {row["model_name"] for row in config["model_list"]}
    if value == "default":
        value = model_defaults(config)["ANTHROPIC_MODEL"]
    elif value in {"opus", "sonnet", "haiku", "fable", "opus[1m]", "sonnet[1m]", "fable[1m]"}:
        family, suffix = (value.split("[", 1)[0], "[1m]" if value.endswith("[1m]") else "")
        slot = next(name for name, candidate in FAMILY_VARS if candidate == family)
        value = env.get(slot, "") + suffix
    target = aliases.get(value, {}).get("model", value)
    if target not in served or (family and not target.startswith(f"claude-{family}-")):
        raise services.ServiceError(f"Claude model '{requested}' is not offered by this Copilot account")
    return value


def explicit_model(argv):
    chosen = None
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--":
            break
        if arg == "--model":
            index += 1
            if index >= len(argv) or argv[index].startswith("-"):
                raise services.ServiceError("--model requires a value")
            chosen = argv[index]
        elif arg.startswith("--model="):
            chosen = arg.partition("=")[2]
        index += 1
    return chosen


def run(command, env, *, wait=False):
    try:
        if os.name == "posix" and not wait:
            os.execvpe(command[0], command, env)
        import signal
        if os.name == "nt":
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            return subprocess.call(command, env=env)
        child = subprocess.Popen(command, env=env)

        def forward(signum, _frame):
            try:
                child.send_signal(signum)
            except ProcessLookupError:
                pass

        previous = signal.signal(signal.SIGTERM, forward)
        try:
            try:
                result = child.wait()
            except KeyboardInterrupt:
                forward(signal.SIGINT, None)
                result = child.wait()
            return result if result >= 0 else 128 - result
        finally:
            signal.signal(signal.SIGTERM, previous)
    except OSError as error:
        gateway.eprint(f"ERROR: cannot run {command[0]}: {error}")
        return 127


def main(argv=None):
    gateway.safe_console()
    argv = list(sys.argv[1:] if argv is None else argv)
    do_refresh, skip_permissions, sync_picker = True, True, False
    while argv and argv[0] in ("--no-refresh", "--respect-permissions", "--sync-model-picker"):
        option = argv.pop(0)
        if option == "--no-refresh":
            do_refresh = False
        elif option == "--respect-permissions":
            skip_permissions = False
        else:
            sync_picker = True
    if gateway.is_help(argv):
        gateway.eprint(__doc__)
        executable = os.environ.get("CCP_CLAUDE") or shutil.which("claude")
        return run([executable, *argv], dict(os.environ)) if executable else 0
    settings = Settings.load()
    try:
        extra = {"require_refresh": True} if sync_picker else {}
        gateway.ensure(settings, ["claude"], do_refresh=do_refresh, **extra)
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
    args = claude_args(argv, env["ANTHROPIC_MODEL"], skip_permissions=skip_permissions)
    if not sync_picker:
        return run([executable, *args], env)
    try:
        model_picker.require_version(executable, "Claude Code", model_picker.CLAUDE_MIN_VERSION)
        chosen = explicit_model(args)
        if chosen is not None:
            picker_model(chosen, env, config)
        env["ANTHROPIC_DEFAULT_MODEL"] = picker_model(
            env.get("ANTHROPIC_DEFAULT_MODEL") or env["ANTHROPIC_MODEL"], env, config)
        directory = settings.home / "claude-picker"
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="settings-", dir=directory) as temp:
            args = model_picker.claude_settings_args(args, config, os.path.join(temp, "settings.json"))
            return run([executable, *args], env, wait=True)
    except (services.ServiceError, OSError) as error:
        gateway.eprint(f"ERROR: {error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
