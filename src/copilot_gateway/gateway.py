"""High-level gateway operations shared by ccgw, ccp and ccx."""
from __future__ import annotations

import os
import sys

from . import catalog, copilot_api, services
from .settings import Settings, write_private

TARGETS = ("claude", "codex")


def eprint(*args):
    print(*args, file=sys.stderr, flush=True)


def safe_console():
    """Never crash on a legacy console codepage (e.g. cp949) over a non-ASCII character."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


HELP_FLAGS = ("-h", "--help", "help", "-V", "--version")


def is_help(argv) -> bool:
    """Help/version requests must not start services (or install anything)."""
    return bool(argv) and argv[0] in HELP_FLAGS


def build_services(settings: Settings, key: str | None = None) -> dict:
    key = key or settings.master_key()
    litellm_env = {"LITELLM_MASTER_KEY": key, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
                   "GITHUB_COPILOT_TOKEN_DIR": str(settings.token_dir)}

    def capi_command():
        return copilot_api.prepare(settings.home, settings.token_dir, settings.capi_port)

    def litellm_command(config, port):
        return lambda: [sys.executable, "-m", "copilot_gateway.litellm_runner",
                        "--config", str(config), "--port", str(port)]

    return {
        "capi": services.Service("capi", settings.capi_port, settings.logs / "copilot-api.log", "capi",
                                 command=capi_command, env={"HOST": "127.0.0.1"}),
        "claude": services.Service("claude", settings.claude_port, settings.logs / "litellm-claude.log",
                                   "litellm", config=settings.claude_config,
                                   command=litellm_command(settings.claude_config, settings.claude_port),
                                   env=litellm_env, auth=key),
        "codex": services.Service("codex", settings.codex_port, settings.logs / "litellm-codex.log",
                                  "litellm", config=settings.codex_config,
                                  command=litellm_command(settings.codex_config, settings.codex_port),
                                  env=litellm_env, auth=key),
    }


def _write_if_changed(path, text) -> bool:
    try:
        if path.read_text(encoding="utf-8") == text:
            return False
    except FileNotFoundError:
        pass
    else:
        backup = path.with_name(path.name + ".bak")
        backup.write_bytes(path.read_bytes())
    write_private(path, text)
    return True


def refresh(settings: Settings, value=None) -> dict:
    """Fetch the live catalog and replace both configs only if both validate (fail-closed)."""
    value = catalog.fetch_catalog(settings.token_dir) if value is None else value
    rendered = {
        "claude": catalog.render(catalog.build_config(value, settings.capi_base)),
        "codex": catalog.render(catalog.build_codex_config(value, settings.capi_base)),
    }
    settings.home.mkdir(parents=True, exist_ok=True)
    return {
        "claude": _write_if_changed(settings.claude_config, rendered["claude"]),
        "codex": _write_if_changed(settings.codex_config, rendered["codex"]),
    }


def config_for(settings, target):
    return settings.claude_config if target == "claude" else settings.codex_config


def ensure(settings: Settings, targets, do_refresh=True, log=eprint) -> dict:
    """Make sure copilot-api and the requested LiteLLM instances run current configs."""
    key = settings.master_key()
    copilot_api.ensure_installed(settings.home, log=log)
    changed = {target: False for target in TARGETS}
    if do_refresh:
        log("🔄 Refreshing Copilot model catalog...")
        try:
            changed = refresh(settings)
            log("   " + ", ".join(f"{t}: {'updated' if changed[t] else 'unchanged'}" for t in TARGETS))
        except (catalog.CatalogError, OSError) as error:
            missing = [t for t in targets if not config_for(settings, t).exists()]
            if missing:
                raise services.ServiceError(f"model refresh failed ({error}) and no existing config for "
                                            + ", ".join(missing)) from None
            log(f"⚠️  Model refresh failed ({error}) — continuing with existing config")
    for target in targets:
        if not config_for(settings, target).exists():
            raise services.ServiceError(f"no {config_for(settings, target)}; run `ccgw refresh`")
    table = build_services(settings, key)
    services.start(table["capi"], log=log)
    for target in targets:
        service = table[target]
        status = services.inspect(service).status
        if status == "legacy" or (status == "running" and changed[target]):
            services.stop(service, log=log)
        services.start(service, log=log)
        log(f"{target:<8}: {services.check_ready(service)} models exposed")
    return table


def child_env(base=None, **extra):
    env = dict(os.environ if base is None else base)
    env.update({key: value for key, value in extra.items() if value is not None})
    return env
