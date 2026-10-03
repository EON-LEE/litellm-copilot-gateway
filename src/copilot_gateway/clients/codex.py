"""ccx — launch OpenAI Codex (CLI or VS Code extension) against the Copilot gateway.

    ccx                         # interactive Codex TUI
    ccx exec "fix the tests"    # any codex arguments pass through
    ccx -m claude-sonnet-5      # any Copilot model id (plain upstream ids)
    ccx code [path]             # VS Code (separate profile) with the Codex extension on the gateway
    ccx --no-refresh ...        # skip the catalog refresh

Codex runs with an isolated CODEX_HOME (<data>/codex), so your normal
~/.codex setup (ChatGPT login, providers) is untouched.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import sys
import tomllib

from .. import catalog, gateway, services
from ..settings import Settings
from .claude import run

PROVIDER = "copilot_gateway"
TOP_BEGIN = "# >>> ccgw managed: provider selection (rewritten by ccx) >>>"
TOP_END = "# <<< ccgw managed: provider selection <<<"
TABLE_BEGIN = "# >>> ccgw managed: gateway provider (rewritten by ccx) >>>"
TABLE_END = "# <<< ccgw managed: gateway provider <<<"
PREFERRED = ("gpt-5.5", "gpt-5.4", "gpt-5-mini")


def default_model(settings, environ=None):
    environ = os.environ if environ is None else environ
    if environ.get("CCX_MODEL"):
        return environ["CCX_MODEL"]
    config = catalog.parse_rendered(settings.codex_config.read_text(encoding="utf-8"))
    names = [row["model_name"] for row in config["model_list"]]
    for name in PREFERRED:
        if name in names:
            return name
    responses = [row["model_name"] for row in config["model_list"]
                 if row["litellm_params"]["model"].startswith("openai/")]
    return (responses or names)[0]


def _strip_block(text, begin, end):
    pattern = re.compile(re.escape(begin) + r".*?" + re.escape(end) + r"\n?", re.DOTALL)
    return pattern.sub("", text)


def render_codex_config(existing, port, model):
    """Merge the gateway provider into config.toml, keeping Codex's own edits (trust, /model)."""
    rest = _strip_block(_strip_block(existing, TOP_BEGIN, TOP_END), TABLE_BEGIN, TABLE_END).strip("\n")
    try:
        parsed = tomllib.loads(rest)
    except tomllib.TOMLDecodeError as error:
        raise services.ServiceError(f"invalid Codex config.toml outside the managed block: {error}") from None
    if "model_provider" in parsed or PROVIDER in parsed.get("model_providers", {}):
        raise services.ServiceError("Codex config.toml must not set model_provider or "
                                    f"model_providers.{PROVIDER} outside the ccgw managed block")
    top = f'{TOP_BEGIN}\nmodel_provider = "{PROVIDER}"\n{TOP_END}\n'
    if "model" not in parsed:
        top += f'model = "{model}"\n'
    table = (f"{TABLE_BEGIN}\n[model_providers.{PROVIDER}]\n"
             'name = "GitHub Copilot (ccgw)"\n'
             f'base_url = "http://127.0.0.1:{port}/v1"\n'
             'env_key = "LITELLM_MASTER_KEY"\n'
             'wire_api = "responses"\n'
             "stream_idle_timeout_ms = 600000\n"
             f"{TABLE_END}\n")
    text = top + ("\n" + rest + "\n" if rest else "") + "\n" + table
    tomllib.loads(text)
    return text


def write_codex_home(settings, model):
    home = settings.codex_home
    home.mkdir(parents=True, exist_ok=True)
    path = home / "config.toml"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    updated = render_codex_config(existing, settings.codex_port, model)
    if updated != existing:
        path.write_text(updated, encoding="utf-8", newline="\n")
    return home


def _version_key(path):
    match = re.search(r"-(\d+(?:\.\d+)*)", path.name)
    return tuple(int(part) for part in match.group(1).split(".")) if match else ()


def vscode_bundled_codex():
    """Codex binary shipped inside the VS Code extension (openai.chatgpt)."""
    name = "codex.exe" if os.name == "nt" else "codex"
    candidates = []
    for base in (".vscode", ".vscode-insiders", ".vscode-server", ".cursor"):
        for extension in (Path.home() / base / "extensions").glob("openai.chatgpt-*"):
            candidates.extend((extension, binary) for binary in (extension / "bin").glob(f"*/{name}"))
    if not candidates:
        return None
    return str(max(candidates, key=lambda item: _version_key(item[0]))[1])


def winget_codex(environ=None):
    """`winget install OpenAI.Codex` binary; its `codex` alias is missing when symlinks are not allowed."""
    environ = os.environ if environ is None else environ
    if sys.platform != "win32" or not environ.get("LOCALAPPDATA"):
        return None
    packages = Path(environ["LOCALAPPDATA"]) / "Microsoft" / "WinGet" / "Packages"
    found = sorted(path for path in packages.glob("OpenAI.Codex_*/codex-*-pc-windows-msvc.exe")
                   if path.is_file())
    return str(found[0]) if found else None


def find_codex(environ=None):
    environ = os.environ if environ is None else environ
    return (environ.get("CCX_CODEX") or shutil.which("codex") or winget_codex(environ)
            or vscode_bundled_codex())


def find_vscode(environ=None):
    environ = os.environ if environ is None else environ
    if environ.get("CCX_VSCODE"):
        return environ["CCX_VSCODE"]
    found = shutil.which("code")
    if found:
        return found
    if os.name == "nt":
        for base in (environ.get("LOCALAPPDATA"), environ.get("ProgramFiles")):
            if base:
                candidate = Path(base) / ("Programs" if base == environ.get("LOCALAPPDATA") else "") \
                    / "Microsoft VS Code" / "bin" / "code.cmd"
                if candidate.exists():
                    return str(candidate)
    return None


def codex_env(settings, key, base=None):
    env = dict(os.environ if base is None else base)
    env.update({"CODEX_HOME": str(settings.codex_home), "LITELLM_MASTER_KEY": key})
    return env


def main(argv=None):
    gateway.safe_console()
    argv = list(sys.argv[1:] if argv is None else argv)
    if gateway.is_help(argv):
        gateway.eprint(__doc__)
        executable = find_codex()
        return run([executable, *argv], dict(os.environ)) if executable else 0
    do_refresh = True
    if argv[:1] == ["--no-refresh"]:
        do_refresh, argv = False, argv[1:]
    settings = Settings.load()
    try:
        gateway.ensure(settings, ["codex"], do_refresh=do_refresh)
        write_codex_home(settings, default_model(settings))
    except (services.ServiceError, RuntimeError, OSError) as error:
        gateway.eprint(f"ERROR: {error}")
        return 1
    env = codex_env(settings, settings.master_key())
    if argv[:1] == ["code"]:
        code = find_vscode()
        if not code:
            gateway.eprint("ERROR: VS Code (`code`) not found; set CCX_VSCODE to its CLI path")
            return 1
        # A separate user-data-dir forces a new VS Code instance that inherits this env.
        profile = settings.home / "vscode-profile"
        gateway.eprint(f"Opening VS Code (profile {profile}); Codex extension uses CODEX_HOME={settings.codex_home}")
        return run([code, "--user-data-dir", str(profile), *argv[1:]], env)
    executable = find_codex()
    if not executable:
        gateway.eprint("ERROR: Codex not found. Install it (`npm i -g @openai/codex`) or the VS Code "
                       "extension openai.chatgpt, or set CCX_CODEX")
        return 1
    return run([executable, *argv], env)


if __name__ == "__main__":
    sys.exit(main())
