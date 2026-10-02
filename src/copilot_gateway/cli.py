"""ccgw — manage the local Copilot gateway (same commands on Windows, macOS, Linux, WSL)."""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time

from . import __version__, catalog, copilot_api, gateway, litellm_patches, services
from .settings import Settings

SERVICES = ("capi", "claude", "codex")


def service_name(value):
    if value not in SERVICES:
        raise argparse.ArgumentTypeError(f"choose from {', '.join(SERVICES)}")
    return value


def cmd_paths(settings, args):
    for label, value in (("data", settings.home), ("env", settings.env_file),
                         ("claude config", settings.claude_config), ("codex config", settings.codex_config),
                         ("logs", settings.logs), ("codex home", settings.codex_home),
                         ("copilot-api", copilot_api.install_root(settings.home)),
                         ("github token", settings.token_dir)):
        print(f"{label:<14} {value}")
    return 0


def cmd_login(settings, args):
    litellm_patches.install()
    os.environ["GITHUB_COPILOT_TOKEN_DIR"] = str(settings.token_dir)
    from litellm.llms.github_copilot.authenticator import Authenticator
    Authenticator().get_api_key()
    print(f"GitHub Copilot login stored in {settings.token_dir}")
    return 0


def cmd_refresh(settings, args):
    changed = gateway.refresh(settings)
    for target, path in (("claude", settings.claude_config), ("codex", settings.codex_config)):
        rows = catalog.parse_rendered(path.read_text(encoding="utf-8"))["model_list"]
        print(f"{target:<7} {'updated' if changed[target] else 'unchanged':<9} {len(rows)} models  {path}")
    if any(changed.values()):
        print("Apply with: ccgw restart")
    return 0


def cmd_models(settings, args):
    print(catalog.format_models(catalog.build_codex_config(catalog.fetch_catalog(settings.token_dir))))
    print("\nClaude Code (ccp) names: claude-<id> (Claude ids unchanged). Codex (ccx) names: the Copilot ID.")
    return 0


def _targets(args):
    if args.services:
        return [name for name in args.services]
    return ["capi", "claude", "codex"] if args.all else ["capi", "claude"]


def cmd_start(settings, args):
    targets = [name for name in _targets(args) if name != "capi"]
    gateway.ensure(settings, targets, do_refresh=not args.no_refresh, log=print)
    if not targets:
        services.start(gateway.build_services(settings)["capi"])
    return 0


def cmd_stop(settings, args):
    table = gateway.build_services(settings)
    names = args.services or ["claude", "codex", "capi"]
    stopped = False
    for name in names:
        stopped |= services.stop(table[name])
    if not stopped:
        print("nothing running")
    return 0


def cmd_restart(settings, args):
    table = gateway.build_services(settings)
    targets = [name for name in _targets(args) if name != "capi"]
    for name in targets:
        services.stop(table[name])
    if args.services and "capi" in args.services:
        services.stop(table["capi"])
    gateway.ensure(settings, targets, do_refresh=not args.no_refresh, log=print)
    return 0


def cmd_status(settings, args):
    table = gateway.build_services(settings)
    code = 0
    for name in SERVICES:
        service = table[name]
        state = services.inspect(service)
        line = f"{name:<7} :{service.port:<6} {state.status:<8} {state.detail}"
        if state.status == "running":
            try:
                line += f"  ({services.check_ready(service)} models)"
            except services.ServiceError as error:
                line += f"  NOT READY: {error}"
                code = 1
        print(line.rstrip())
    return code


def cmd_logs(settings, args):
    table = gateway.build_services(settings)
    path = table[args.service].log
    if not path.exists():
        print(f"no log yet at {path}")
        return 0
    with open(path, encoding="utf-8", errors="replace") as handle:
        lines = handle.readlines()
        sys.stdout.writelines(lines[-args.lines:])
        if not args.follow:
            return 0
        try:
            while True:
                line = handle.readline()
                if line:
                    sys.stdout.write(line)
                    sys.stdout.flush()
                else:
                    time.sleep(0.5)
        except KeyboardInterrupt:
            return 0


def cmd_env(settings, args):
    """Print the client environment, e.g. for IDE extensions or manual runs."""
    key = settings.master_key()
    if args.client == "claude":
        values = {"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{settings.claude_port}", "ANTHROPIC_AUTH_TOKEN": key,
                  "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1"}
    else:
        values = {"CODEX_HOME": str(settings.codex_home), "LITELLM_MASTER_KEY": key}
    shell = args.shell or ("powershell" if os.name == "nt" else "posix")
    for name, value in values.items():
        if shell == "powershell":
            print(f"$env:{name} = '{value}'")
        elif shell == "cmd":
            print(f"set {name}={value}")
        else:
            print(f"export {name}='{value}'")
    return 0


def cmd_doctor(settings, args):
    from .clients import codex
    ok = True

    def report(label, good, detail=""):
        nonlocal ok
        ok &= bool(good)
        print(f"[{'ok' if good else '!!'}] {label:<22} {detail}")

    report("python", sys.version_info >= (3, 11), sys.version.split()[0])
    try:
        litellm_patches.install()
        report("litellm patches", True, ", ".join(litellm_patches.verify()))
    except Exception as error:  # noqa: BLE001 - doctor must report every failure
        report("litellm patches", False, str(error))
    report("node", shutil.which("node"), shutil.which("node") or "missing (Node.js 20+ required)")
    try:
        copilot_api.verify_package(copilot_api.package_dir(settings.home))
        report("copilot-api", True, f"{copilot_api.EXPECTED_VERSION} at {copilot_api.install_root(settings.home)}")
    except copilot_api.CopilotApiError as error:
        report("copilot-api", False, f"{error} (run `ccgw setup`)")
    token = settings.token_dir / "access-token"
    report("github login", token.exists() and token.stat().st_size > 0, str(token))
    report("master key", bool(settings.file_values.get("LITELLM_MASTER_KEY")), str(settings.env_file))
    report("claude config", settings.claude_config.exists(), str(settings.claude_config))
    report("codex config", settings.codex_config.exists(), str(settings.codex_config))
    claude = shutil.which("claude")
    print(f"[--] {'claude (ccp)':<22} {claude or 'not installed (optional)'}")
    print(f"[--] {'codex (ccx)':<22} {codex.find_codex() or 'not installed (optional)'}")
    print(f"[--] {'vscode (ccx code)':<22} {codex.find_vscode() or 'not installed (optional)'}")
    return 0 if ok else 1


def cmd_setup(settings, args):
    key_existed = bool(settings.file_values.get("LITELLM_MASTER_KEY"))
    settings.master_key()
    print(f"master key     {'kept' if key_existed else 'created'} in {settings.env_file}")
    copilot_api.ensure_installed(settings.home)
    print(f"copilot-api    {copilot_api.EXPECTED_VERSION} ready")
    litellm_patches.install()
    litellm_patches.verify()
    print("litellm        patches verified")
    token = settings.token_dir / "access-token"
    if not (token.exists() and token.stat().st_size > 0):
        # Device login runs in a child so this process keeps unpatched imports out of sys.modules.
        result = subprocess.call([sys.executable, "-m", "copilot_gateway.cli", "login"])
        if result:
            return result
    return cmd_refresh(settings, args)


def build_parser():
    parser = argparse.ArgumentParser(prog="ccgw", description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("setup", help="create key, install copilot-api, log in, generate configs").set_defaults(func=cmd_setup)
    sub.add_parser("login", help="GitHub device login for Copilot").set_defaults(func=cmd_login)
    sub.add_parser("refresh", help="regenerate configs from the live Copilot catalog").set_defaults(func=cmd_refresh)
    sub.add_parser("models", help="list Copilot models and token limits").set_defaults(func=cmd_models)
    for name, func, help_text in (("start", cmd_start, "start services (default: capi + claude)"),
                                  ("restart", cmd_restart, "restart LiteLLM instances (config reload)")):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("services", nargs="*", type=service_name, default=[])
        command.add_argument("--all", action="store_true", help="include the Codex instance")
        command.add_argument("--no-refresh", action="store_true")
        command.set_defaults(func=func)
    stop = sub.add_parser("stop", help="stop verified gateway processes (default: all)")
    stop.add_argument("services", nargs="*", type=service_name, default=[])
    stop.set_defaults(func=cmd_stop)
    sub.add_parser("status", help="show ports, owners and readiness").set_defaults(func=cmd_status)
    logs = sub.add_parser("logs", help="show a service log")
    logs.add_argument("service", nargs="?", default="claude", choices=SERVICES)
    logs.add_argument("-n", "--lines", type=int, default=50)
    logs.add_argument("-f", "--follow", action="store_true")
    logs.set_defaults(func=cmd_logs)
    env = sub.add_parser("env", help="print client environment variables")
    env.add_argument("client", choices=("claude", "codex"))
    env.add_argument("--shell", choices=("posix", "powershell", "cmd"))
    env.set_defaults(func=cmd_env)
    sub.add_parser("doctor", help="check prerequisites and patches").set_defaults(func=cmd_doctor)
    sub.add_parser("paths", help="show data locations").set_defaults(func=cmd_paths)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    settings = Settings.load()
    try:
        return args.func(settings, args)
    except (services.ServiceError, catalog.CatalogError, copilot_api.CopilotApiError,
            litellm_patches.PatchError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
