"""Global Codex setup (~/.codex) so plain `codex` — e.g. Orca's default Codex agent — uses the gateway.

`ccgw codex-setup` writes:

* ``<data>/codex-model-catalog.json`` — a Codex ``model_catalog_json`` listing exactly this
  account's models. Models Codex bundles keep their bundled metadata; other models get the
  metadata Codex already uses for unknown models (captured once per Codex version), so only
  the model picker changes.
* managed blocks in ``$CODEX_HOME/config.toml`` (default ``~/.codex``) selecting the gateway
  provider and that catalog. Everything else in the file is preserved.

``ccgw refresh`` and the periodic catalog refresh regenerate the catalog once it exists.
"""
from __future__ import annotations

import http.server
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import tomllib

from .. import catalog, services
from ..settings import write_private
from . import model_picker

PROVIDER = "copilot_gateway"
TOP_BEGIN = "# >>> ccgw managed: global Codex (rewritten by `ccgw codex-setup`) >>>"
TOP_END = "# <<< ccgw managed: global Codex <<<"
TABLE_BEGIN = "# >>> ccgw managed: global Codex provider (rewritten by `ccgw codex-setup`) >>>"
TABLE_END = "# <<< ccgw managed: global Codex provider <<<"
PROBE_MODEL = "ccgw-unknown-model-probe"
PROBE_TIMEOUT = 120

# Codex's metadata for models missing from its catalog, except the base instructions
# (captured from Codex itself). Verified against Codex 0.167 requests: identical prompt,
# tools and reasoning parameters apart from the sub-agent model list.
UNKNOWN_MODEL_FIELDS = {
    "default_reasoning_level": None,
    "shell_type": "unified_exec",
    "visibility": "list",
    "supported_in_api": True,
    "additional_speed_tiers": [],
    "service_tiers": [],
    "availability_nux": None,
    "upgrade": None,
    "include_skills_usage_instructions": False,
    "include_plugin_usage_instructions": False,
    "include_apps_usage_instructions": False,
    "default_reasoning_summary": "auto",
    "support_verbosity": False,
    "default_verbosity": None,
    "apply_patch_tool_type": None,
    "web_search_tool_type": "text",
    "truncation_policy": {"mode": "bytes", "limit": 10000},
    "supports_image_detail_original": False,
    "effective_context_window_percent": 95,
    "experimental_supported_tools": [],
    "supports_search_tool": False,
    "supports_experimental_context": False,
    "use_responses_lite": False,
    "supports_reasoning_effort_updates": False,
    "node_repl_auto_review_required": False,
    "node_repl_disabled": False,
}


def catalog_path(settings) -> Path:
    return settings.home / "codex-model-catalog.json"


def global_home(environ=None) -> Path:
    environ = os.environ if environ is None else environ
    raw = environ.get("CODEX_HOME")
    return Path(raw).expanduser() if raw else Path.home() / ".codex"


def build_catalog(config, copilot_payload, bundled_payload, unknown_instructions):
    """Codex catalog: account models listed in gateway order; other bundled models hidden."""
    bundled = model_picker._index_models(bundled_payload, "models", "slug")
    models = []
    for priority, (model_id, info, _raw, support, effort) in enumerate(
            model_picker.account_models(config, copilot_payload)):
        original = bundled.pop(model_id, None)
        if original:
            entry = dict(original)
            levels = model_picker.bundled_levels(original, model_id, effort)
            entry["supported_reasoning_levels"] = levels
            efforts = [level["effort"] for level in levels]
            if entry.get("default_reasoning_level") not in efforts:
                entry["default_reasoning_level"] = (
                    "medium" if "medium" in efforts else efforts[0] if efforts else None)
        else:
            window = info.get("max_input_tokens")
            entry = dict(UNKNOWN_MODEL_FIELDS)
            entry.update({
                "slug": model_id,
                "supported_reasoning_levels": [
                    {"effort": level, "description": f"{level.capitalize()} reasoning"} for level in effort],
                "input_modalities": ["text", "image"] if support.get("vision") is True else ["text"],
                "context_window": window if isinstance(window, int) and window > 0 else None,
                "base_instructions": unknown_instructions,
            })
            entry["max_context_window"] = entry["context_window"]
        entry.update({"display_name": info["display_name"], "description": info["description"],
                      "visibility": "list", "priority": priority})
        models.append(entry)
    if not models:
        raise services.ServiceError("no Copilot models are available for the Codex catalog")
    # Not offered by this account: keep Codex's metadata but out of the picker.
    for entry in bundled.values():
        models.append({**entry, "visibility": "hide"})
    return {"models": models}


def _codex_version(executable):
    result = subprocess.run([executable, "--version"], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=30, check=False)
    match = re.search(r"\b\d+\.\d+\.\d+\S*", result.stdout)
    if result.returncode or not match:
        raise services.ServiceError("cannot read the Codex version")
    return match.group(0)


def probe_unknown_instructions(executable, timeout=PROBE_TIMEOUT):
    """Base instructions Codex sends for a model outside its catalog, read from one local request.

    Codex talks only to a throwaway 127.0.0.1 server (no credentials) from a temporary
    CODEX_HOME and working directory.
    """
    captured = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("content-length") or 0)
            try:
                captured.append(json.loads(self.rfile.read(length)))
            except (UnicodeError, json.JSONDecodeError):
                pass
            self.send_response(500)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="ccgw-codex-probe-", ignore_cleanup_errors=True) as temp:
            home, work = Path(temp) / "home", Path(temp) / "work"
            home.mkdir()
            work.mkdir()
            (home / "config.toml").write_text(
                'model_provider = "probe"\n[model_providers.probe]\nname = "probe"\n'
                f'base_url = "http://127.0.0.1:{server.server_address[1]}/v1"\nwire_api = "responses"\n'
                "request_max_retries = 0\nstream_max_retries = 0\n", encoding="utf-8")
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith(("OPENAI_", "CODEX_"))}
            env["CODEX_HOME"] = str(home)
            try:
                subprocess.run([executable, "exec", "--skip-git-repo-check", "-m", PROBE_MODEL, "probe"],
                               cwd=work, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                               timeout=timeout, check=False)
            except (OSError, subprocess.SubprocessError) as error:
                raise services.ServiceError(f"cannot run Codex to read its default instructions: {error}") from None
    finally:
        server.shutdown()
        server.server_close()
    for body in captured:
        if body.get("model") != PROBE_MODEL or not isinstance(body.get("input"), list) or not body["input"]:
            continue
        first = body["input"][0]
        parts = first.get("content") if isinstance(first, dict) else None
        if (first.get("role") == "developer" and isinstance(parts, list) and len(parts) == 1
                and isinstance(parts[0], dict) and parts[0].get("type") == "input_text"
                and isinstance(parts[0].get("text"), str) and parts[0]["text"].strip()
                and not parts[0]["text"].lstrip().startswith("<")):
            return parts[0]["text"]
    raise services.ServiceError("could not read Codex's default model instructions")


def unknown_instructions(settings, executable):
    """Cached per Codex version, since the probe starts Codex once."""
    version = _codex_version(executable)
    cache = settings.home / "codex-unknown-model-instructions.json"
    try:
        cached = json.loads(cache.read_text(encoding="utf-8"))
        if cached.get("codex_version") == version and isinstance(cached.get("text"), str) and cached["text"]:
            return cached["text"]
    except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
        pass
    text = probe_unknown_instructions(executable)
    write_private(cache, json.dumps({"codex_version": version, "text": text}, ensure_ascii=False))
    return text


def write_catalog(settings, executable) -> bool:
    """Regenerate the catalog file; True when its content changed."""
    model_picker.require_version(executable, "Codex", model_picker.CODEX_MIN_VERSION)
    config = catalog.parse_rendered(settings.codex_config.read_text(encoding="utf-8"))
    env = {key: value for key, value in os.environ.items() if key != "CODEX_HOME"}
    value = build_catalog(config, model_picker._read_capi(settings),
                          model_picker._read_bundled_codex(executable, env),
                          unknown_instructions(settings, executable))
    text = json.dumps(value, ensure_ascii=False, indent=1) + "\n"
    path = catalog_path(settings)
    try:
        if path.read_text(encoding="utf-8") == text:
            return False
    except OSError:
        pass
    write_private(path, text)
    return True


def _toml_string(value):
    # JSON string escapes are valid TOML basic-string escapes.
    return json.dumps(str(value), ensure_ascii=False)


def _take_block(text, begin, end):
    """Remove a managed block; return (text without it, block body)."""
    pattern = re.compile(re.escape(begin) + r"\n?(.*?)" + re.escape(end) + r"\n?", re.DOTALL)
    match = pattern.search(text)
    if not match:
        return text, ""
    return text[:match.start()] + text[match.end():], match.group(1)


def _foreign_tables(body):
    """Tables Codex itself appended inside the provider block (e.g. [tui]); they are kept."""
    kept, keep = [], False
    for line in body.splitlines():
        header = re.match(r"\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*(#.*)?$", line)
        if header:
            keep = header.group(1) != f"model_providers.{PROVIDER}"
        if keep:
            kept.append(line)
    return "\n".join(kept).strip("\n")


def render_global_config(existing, port, key, catalog_file, model):
    rest, _ = _take_block(existing, TOP_BEGIN, TOP_END)
    rest, provider_body = _take_block(rest, TABLE_BEGIN, TABLE_END)
    rest = rest.strip("\n")
    foreign = _foreign_tables(provider_body)
    if foreign:
        rest = (rest + "\n\n" + foreign) if rest else foreign
    try:
        parsed = tomllib.loads(rest)
    except tomllib.TOMLDecodeError as error:
        raise services.ServiceError(f"invalid Codex config.toml outside the ccgw blocks: {error}") from None
    clashes = [name for name in ("model_provider", "model_catalog_json") if name in parsed]
    if PROVIDER in parsed.get("model_providers", {}):
        clashes.append(f"[model_providers.{PROVIDER}]")
    if clashes:
        raise services.ServiceError("remove " + ", ".join(clashes) + " from Codex config.toml; "
                                    "`ccgw codex-setup` manages them")
    top = (f"{TOP_BEGIN}\nmodel_provider = \"{PROVIDER}\"\n"
           f"model_catalog_json = {_toml_string(Path(catalog_file).as_posix())}\n{TOP_END}\n")
    if "model" not in parsed and model:
        top += f"model = {_toml_string(model)}\n"
    table = (f"{TABLE_BEGIN}\n[model_providers.{PROVIDER}]\n"
             'name = "GitHub Copilot (ccgw)"\n'
             f'base_url = "http://127.0.0.1:{port}/v1"\n'
             'wire_api = "responses"\n'
             f"experimental_bearer_token = {_toml_string(key)}\n"
             "stream_idle_timeout_ms = 600000\n"
             f"{TABLE_END}\n")
    text = top + ("\n" + rest + "\n" if rest else "") + "\n" + table
    tomllib.loads(text)
    return text


def write_global_config(settings, home=None) -> Path:
    home = global_home() if home is None else Path(home)
    if home.resolve() == settings.codex_home.resolve():
        raise services.ServiceError(f"CODEX_HOME points at ccx's private home ({home}); unset it first")
    path = home / "config.toml"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    config = catalog.parse_rendered(settings.codex_config.read_text(encoding="utf-8"))
    updated = render_global_config(existing, settings.codex_port, settings.master_key(),
                                   catalog_path(settings), catalog.codex_default(config))
    if updated != existing:
        if existing:
            backup = path.with_name("config.toml.ccgw-backup")
            if not backup.exists():
                write_private(backup, existing)
        write_private(path, updated)
    return path


def saved_global_model(home=None):
    home = global_home() if home is None else Path(home)
    try:
        value = tomllib.loads((home / "config.toml").read_text(encoding="utf-8")).get("model")
    except (OSError, tomllib.TOMLDecodeError):
        return None
    return value if isinstance(value, str) else None


def refresh_if_installed(settings):
    """After a catalog refresh: regenerate the Codex catalog once `ccgw codex-setup` created it.

    Returns a status line, or None when global Codex setup is not in use.
    """
    if not catalog_path(settings).exists():
        return None
    from .codex import find_codex
    executable = find_codex()
    if not executable:
        return "codex catalog not updated: Codex not found"
    try:
        changed = write_catalog(settings, executable)
    except (services.ServiceError, OSError, subprocess.SubprocessError) as error:
        return f"codex catalog not updated: {error}"
    return f"codex catalog {'updated' if changed else 'unchanged'} ({catalog_path(settings)}); new Codex sessions use it"
