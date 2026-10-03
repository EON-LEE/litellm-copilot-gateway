"""Deterministic copilot-api install, named-model identity guard, and launch command.

copilot-api is installed with npm into a private, version-pinned prefix under
the data dir (no npx cache lookup, no PATH ambiguity). Its bundled server is
patched so a named Claude model is never swapped for Copilot's "small model".
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

PACKAGE = "@jeffreycao/copilot-api"
EXPECTED_VERSION = "2.6.15"
MARKER = "// PATCHED (local): preserve explicitly requested Copilot model"
LEGACY_BRANCH = '''\tif (!state.tokenBasedBilling && !shouldUseClaudeAutoModel) {
\t\tconst tools = anthropicPayload.tools;
\t\tconst noTools = !tools || tools.length === 0;
\t\tif (anthropicBeta && noTools && compactType === 0) anthropicPayload.model = getSmallModel();
\t}
'''
BRANCH = '''\tif (!state.tokenBasedBilling && !shouldUseClaudeAutoModel) {
\t\tconst tools = anthropicPayload.tools;
\t\tconst noTools = !tools || tools.length === 0;
\t\tif (anthropicBeta && noTools && compactType === 0) {
\t\t\tconst smallModel = getSmallModel();
\t\t\tconsola.debug(`Claude Code warmup small model: ${anthropicPayload.model} -> ${smallModel}`);
\t\t\tanthropicPayload.model = smallModel;
\t\t}
\t}
'''
PREFIX = '''\tconst anthropicBeta = c.req.header("anthropic-beta");
\tlogger$9.debug("Anthropic Beta header:", anthropicBeta);
'''
SUFFIX = '\tif (compactType) logger$9.debug("Compact request type:", compactType);\n'


class CopilotApiError(RuntimeError):
    pass


def unique_match(pattern, source, label):
    matches = re.findall(pattern, source)
    if len(matches) != 1:
        raise CopilotApiError(f"unexpected {label}; upstream may have changed")
    return matches[0]


def imported_bundle(source_path, prefix, dynamic=False):
    specifier = r'"(\./' + prefix + r'-[\w-]+\.js)"'
    pattern = r'await import\(' + specifier + r'\)' if dynamic else r'from ' + specifier
    name = unique_match(pattern, source_path.read_text(encoding="utf-8"), f"{prefix} bundle import")
    target = (source_path.parent / name).resolve(strict=True)
    if target.parent != source_path.parent.resolve():
        raise CopilotApiError("imported bundle escapes package directory; upstream may have changed")
    return target


def app_dir(config_source, environ=None):
    """Derive copilot-api's APP_DIR from the installed paths.ts expressions."""
    environ = os.environ if environ is None else environ
    source = config_source.read_text(encoding="utf-8")
    literal = r'"(?:[^"\\]|\\.)*"'
    components = unique_match(
        r'const DEFAULT_DIR = path\.join\(os\.homedir\(\), (' + literal +
        r'(?:, ' + literal + r')*)\);', source, "default config directory",
    )
    env_name = unique_match(r'const APP_DIR = process\.env\.(\w+) \|\| DEFAULT_DIR;',
                            source, "config home expression")
    default_home = Path.home().joinpath(*json.loads("[" + components + "]"))
    return Path(environ.get(env_name) or default_home)


def config_path(config_source, environ=None):
    source = config_source.read_text(encoding="utf-8")
    filename = unique_match(r'CONFIG_PATH: path\.join\(APP_DIR, ("(?:[^"\\]|\\.)*")\)',
                            source, "config path expression")
    return app_dir(config_source, environ) / json.loads(filename)


def check_identity_options(path):
    """Read config without invoking upstream getConfig(), which can write it."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return
    try:
        config = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        raise CopilotApiError("Copilot API config must contain valid JSON") from None
    if not isinstance(config, dict):
        raise CopilotApiError("Copilot API config must be a JSON object")
    mappings = config.get("modelMappings", {})
    auto_model = config.get("claudeAutoModel", "")
    if not isinstance(mappings, dict) or not isinstance(auto_model, str):
        raise CopilotApiError("modelMappings must be an object and claudeAutoModel must be a string")
    if mappings or auto_model.strip():
        raise CopilotApiError(
            "disable cross-model mappings (modelMappings) and claudeAutoModel "
            f"in {path} before starting this gateway; configuration was not changed"
        )


def patch_identity(server):
    source = server.read_text(encoding="utf-8")
    anchors = [PREFIX + branch + SUFFIX for branch in (LEGACY_BRANCH, BRANCH)]
    new = PREFIX + "\t" + MARKER + "\n" + SUFFIX
    assignments = re.findall(r"\banthropicPayload\.model\s*=\s*(?:getSmallModel\b|smallModel\b)", source)
    unique_boundaries = source.count(PREFIX) == 1 and source.count(SUFFIX) == 1
    if unique_boundaries and source.count(new) == 1 and source.count(MARKER) == 1 and not assignments:
        return False
    matches = [anchor for anchor in anchors if source.count(anchor) == 1]
    if not unique_boundaries or MARKER in source or len(matches) != 1 or len(assignments) != 1:
        raise CopilotApiError("unexpected implicit model-substitution anchor; upstream may have changed")
    updated = source.replace(matches[0], new)
    # Replace atomically so another startup cannot load a partly-written bundle.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=server.parent,
                                         prefix=".copilot-identity-", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(updated)
        temporary.chmod(server.stat().st_mode)
        os.replace(temporary, server)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return True


def install_root(home):
    return Path(home) / "copilot-api" / EXPECTED_VERSION


def package_dir(home):
    return install_root(home) / "node_modules" / Path(*PACKAGE.split("/"))


def verify_package(package):
    """Return (entry, server, config) bundles of a verified 2.6.15 install."""
    try:
        manifest = json.loads((package / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise CopilotApiError(f"{PACKAGE} is not installed at {package}") from None
    if not isinstance(manifest, dict) or manifest.get("name") != PACKAGE:
        raise CopilotApiError(f"{package} is not the expected {PACKAGE} package")
    if manifest.get("version") != EXPECTED_VERSION:
        raise CopilotApiError(f"Copilot API must be the verified version {EXPECTED_VERSION}; refusing to start")
    binaries = manifest.get("bin")
    if not isinstance(binaries, dict) or binaries.get("copilot-api") != "./dist/main.js":
        raise CopilotApiError("unexpected Copilot API package executable; upstream may have changed")
    entry = (package / "dist" / "main.js").resolve(strict=True)
    start = imported_bundle(entry, "start", dynamic=True)
    server = imported_bundle(start, "server", dynamic=True)
    config = imported_bundle(start, "config")
    return entry, server, config


def node_executable():
    node = shutil.which("node")
    if not node:
        raise CopilotApiError("Node.js (node) is required on PATH for copilot-api")
    return node


def ensure_installed(home, environ=None, log=print):
    environ = os.environ if environ is None else environ
    package = package_dir(home)
    try:
        verify_package(package)
        return package
    except CopilotApiError:
        pass
    npm = shutil.which("npm")
    if not npm:
        raise CopilotApiError("npm is required to install copilot-api (install Node.js 20+)")
    root = install_root(home)
    root.mkdir(parents=True, exist_ok=True)
    source = environ.get("CCGW_COPILOT_API_SOURCE") or f"{PACKAGE}@{EXPECTED_VERSION}"
    command = [npm, "install", "--prefix", str(root), "--no-audit", "--no-fund", "--omit=dev",
               "--no-save", "--loglevel=error", source]
    registry = environ.get("CCGW_NPM_REGISTRY")
    if registry:
        command[2:2] = ["--registry", registry]
    log(f"Installing {source} into {root} ...")
    result = subprocess.run(command, text=True, capture_output=True)
    if result.returncode != 0:
        raise CopilotApiError(
            f"npm install failed ({result.returncode}): {result.stderr.strip()[-800:]}\n"
            "Hint: set CCGW_NPM_REGISTRY=https://registry.npmjs.org/ if your npm mirror lacks this package, "
            "or CCGW_COPILOT_API_SOURCE=<path to an `npm pack` tarball> for offline installs."
        )
    verify_package(package)
    return package


def seed_token(config_source, token_dir, environ=None):
    """Seed copilot-api's token store from LiteLLM's OAuth token (never via argv)."""
    environ = os.environ if environ is None else environ
    if environ.get("COPILOT_API_OAUTH_APP") or environ.get("COPILOT_API_ENTERPRISE_URL"):
        return
    target = app_dir(config_source, environ) / "github_token"
    if target.exists() and target.stat().st_size > 0:
        return
    source = Path(token_dir) / "access-token"
    try:
        token = source.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        raise CopilotApiError(f"no GitHub Copilot login at {token_dir}; run `ccgw login`") from None
    from .settings import write_private
    write_private(target, token)


TRANSPORT_KEY = "useResponsesApiWebSocket"


def enforce_http_transport(path, config_source=None):
    """Pin copilot-api to HTTP SSE for Responses streaming.

    Copilot's ``ws:/responses`` transport fails mid-stream for some models
    (gpt-5-mini: ``internal_error``) while HTTP SSE works for every model.
    Only this one key is touched; returns True when the file was changed.
    """
    if config_source is not None and \
            f"getConfig().{TRANSPORT_KEY} ?? true" not in config_source.read_text(encoding="utf-8"):
        raise CopilotApiError(f"unexpected {TRANSPORT_KEY} handling; upstream may have changed")
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raw = ""
    config = json.loads(raw) if raw.strip() else {}
    if not isinstance(config, dict):
        raise CopilotApiError("Copilot API config must be a JSON object")
    if config.get(TRANSPORT_KEY) is False:
        return False
    config[TRANSPORT_KEY] = False
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=path.parent,
                                         prefix=".config-", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(json.dumps(config, indent=2) + "\n")
        if path.exists():
            temporary.chmod(path.stat().st_mode)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return True


def prepare(home, token_dir, port, environ=None):
    """Validate + patch the install and return the launch command."""
    entry, server, config = verify_package(package_dir(home))
    path = config_path(config, environ)
    check_identity_options(path)
    enforce_http_transport(path, config)
    patch_identity(server)
    seed_token(config, token_dir, environ)
    return [node_executable(), str(entry), "start", "--port", str(port)]


if __name__ == "__main__":  # pragma: no cover - debugging helper
    print(verify_package(Path(sys.argv[1])))
