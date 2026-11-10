"""High-level gateway operations shared by ccgw, ccp and ccx."""
from __future__ import annotations

import os
import sys
import time

from . import auth, catalog, copilot_api, hot_reload, services
from .settings import Settings, write_private

TARGETS = ("claude", "codex")
# Time a running LiteLLM gets to apply a new model list before it is restarted instead.
HOT_RELOAD_TIMEOUT = 15


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
                   "GITHUB_COPILOT_TOKEN_DIR": str(settings.token_dir),
                   # Same files as the catalog and copilot-api, even if the user's shell overrides them.
                   "GITHUB_COPILOT_ACCESS_TOKEN_FILE": auth.TOKEN_FILE,
                   "GITHUB_COPILOT_API_KEY_FILE": auth.API_KEY_FILE,
                   # The in-proxy catalog refresh must write the same data directory with the same ports.
                   "CCGW_HOME": str(settings.home), "CCGW_PORT": str(settings.claude_port),
                   "CCGW_CODEX_PORT": str(settings.codex_port), "CCGW_CAPI_PORT": str(settings.capi_port)}

    def capi_command():
        return copilot_api.prepare(settings.home, settings.capi_port)

    def capi_secret_env():
        return copilot_api.token_env(auth.read_token(settings.token_dir))

    def litellm_command(name, config, port):
        return lambda: [sys.executable, "-m", "copilot_gateway.litellm_runner",
                        "--config", str(config), "--port", str(port),
                        "--state", str(reload_state(settings, name)),
                        "--refresh-minutes", f"{settings.refresh_minutes:g}"]

    return {
        "capi": services.Service("capi", settings.capi_port, settings.logs / "copilot-api.log", "capi",
                                 command=capi_command, env={"HOST": "127.0.0.1"}, secret_env=capi_secret_env),
        "claude": services.Service("claude", settings.claude_port, settings.logs / "litellm-claude.log",
                                   "litellm", config=settings.claude_config,
                                   command=litellm_command("claude", settings.claude_config, settings.claude_port),
                                   env=litellm_env, auth=key),
        "codex": services.Service("codex", settings.codex_port, settings.logs / "litellm-codex.log",
                                  "litellm", config=settings.codex_config,
                                  command=litellm_command("codex", settings.codex_config, settings.codex_port),
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
        "claude": catalog.render(catalog.build_config(value, settings.capi_base, settings.exclude_models)),
        "codex": catalog.render(catalog.build_codex_config(value, settings.capi_base, settings.exclude_models)),
    }
    settings.home.mkdir(parents=True, exist_ok=True)
    return {
        "claude": _write_if_changed(settings.claude_config, rendered["claude"]),
        "codex": _write_if_changed(settings.codex_config, rendered["codex"]),
    }


def config_for(settings, target):
    return settings.claude_config if target == "claude" else settings.codex_config


def reload_state(settings, name):
    return settings.run / f"{name}.loaded"


def hot_reloaded(settings, service, timeout=HOT_RELOAD_TIMEOUT, poll=0.25) -> bool:
    """True once the running LiteLLM applied the current config in place (no restart needed).

    False when it is not running, predates hot reload (no status from its
    pid), reports that the change needs a restart, or does not catch up in time.
    """
    state = services.inspect(service)
    if state.status != "running":
        return False
    pids = {proc.pid for proc in state.processes}
    try:
        want = hot_reload.digest(service.config.read_text(encoding="utf-8"))
    except OSError:
        return False
    deadline = time.monotonic() + timeout
    while True:
        record = hot_reload.read_state(reload_state(settings, service.name))
        if not record or record.get("pid") not in pids or record.get("needs_restart") == want:
            return False
        if record.get("applied") == want:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll)


def sync_web_search(settings, log=eprint, environ=None) -> bool:
    """Point copilot-api's WebSearch helper at an offered model; True when its config changed."""
    environ = os.environ if environ is None else environ
    try:
        config = catalog.parse_rendered(settings.codex_config.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    override = environ.get("CCGW_WEBSEARCH_MODEL") or settings.file_values.get("CCGW_WEBSEARCH_MODEL")
    result = copilot_api.sync_installed_web_search(settings.home, catalog.web_search_models(config),
                                                   override, environ)
    if result is None:
        return False
    old, new = result
    if new is None:
        log(f"⚠️  WebSearch model {old} is not offered by this Copilot account, so Claude Code WebSearch "
            "will fail. Set CCGW_WEBSEARCH_MODEL to an OpenAI model from `ccgw models`.")
        return False
    log(f"WebSearch: {old} -> {new} (copilot-api {copilot_api.WEB_SEARCH_KEY})")
    return True


def credential_file(settings, name):
    return settings.run / f"{name}.credential"


def _started_with(settings, name):
    try:
        return credential_file(settings, name).read_text(encoding="utf-8").strip()
    except OSError:
        return None


def run_service(settings, service, credential, restart=False, log=eprint) -> None:
    """Start a service; restart it if it runs an old config or a login other than `credential`.

    A running service with no recorded login (an older ccgw, or another
    tool) is restarted too, because its account cannot be confirmed.
    """
    status = services.inspect(service).status
    if status == "legacy" or (status == "running" and (restart or _started_with(settings, service.name) != credential)):
        services.stop(service, log=log)
    if services.start(service, log=log):
        write_private(credential_file(settings, service.name), credential + "\n")


def start_capi(settings, table, credential, log=eprint):
    # copilot-api reads its config once, so a WebSearch model change needs a restart.
    run_service(settings, table["capi"], credential, restart=sync_web_search(settings, log), log=log)


def ensure(settings: Settings, targets, do_refresh=True, log=eprint, require_refresh=False) -> dict:
    """Make sure copilot-api and the requested LiteLLM instances run current configs and the current login."""
    if require_refresh and not do_refresh:
        raise services.ServiceError("model picker synchronization requires a live catalog refresh")
    key = settings.master_key()
    credential = auth.fingerprint(settings.token_dir)
    auth.secure(settings.token_dir)
    copilot_api.ensure_installed(settings.home, log=log)
    changed = {target: False for target in TARGETS}
    if do_refresh:
        log("🔄 Refreshing Copilot model catalog...")
        try:
            changed = refresh(settings)
            log("   " + ", ".join(f"{t}: {'updated' if changed[t] else 'unchanged'}" for t in TARGETS))
        except (catalog.CatalogError, OSError) as error:
            if require_refresh:
                raise services.ServiceError(f"model refresh failed; cannot synchronize the picker: {error}") from None
            missing = [t for t in targets if not config_for(settings, t).exists()]
            if missing:
                raise services.ServiceError(f"model refresh failed ({error}) and no existing config for "
                                            + ", ".join(missing)) from None
            log(f"⚠️  Model refresh failed ({error}) — continuing with existing config")
    for target in targets:
        if not config_for(settings, target).exists():
            raise services.ServiceError(f"no {config_for(settings, target)}; run `ccgw refresh`")
    table = build_services(settings, key)
    start_capi(settings, table, credential, log=log)
    for target in targets:
        service = table[target]
        restart = changed[target] and not hot_reloaded(settings, service)
        if changed[target] and not restart:
            log(f"{target:<8}: new model list applied without restart")
        run_service(settings, service, credential, restart=restart, log=log)
        log(f"{target:<8}: {services.check_ready(service)} models exposed")
    return table


def apply_login(settings, log=eprint) -> list:
    """After a new login, move whatever is running onto it (the credential check restarts them)."""
    table = build_services(settings)
    running = [name for name in ("capi", *TARGETS) if services.inspect(table[name]).status == "running"]
    if running:
        ensure(settings, [name for name in running if name != "capi"], do_refresh=False, log=log)
    return running


def child_env(base=None, **extra):
    env = dict(os.environ if base is None else base)
    env.update({key: value for key, value in extra.items() if value is not None})
    return env
