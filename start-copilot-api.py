#!/usr/bin/env python3
"""Guard named-model identity, patch the PATH-selected package, then exec it."""

import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile


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


def unique_match(pattern, source, label):
    matches = re.findall(pattern, source)
    if len(matches) != 1:
        raise RuntimeError(f"unexpected {label}; upstream may have changed")
    return matches[0]


def imported_bundle(source_path, prefix, dynamic=False):
    specifier = r'"(\./' + prefix + r'-[\w-]+\.js)"'
    pattern = r'await import\(' + specifier + r'\)' if dynamic else r'from ' + specifier
    name = unique_match(pattern, source_path.read_text(), f"{prefix} bundle import")
    target = (source_path.parent / name).resolve(strict=True)
    if target.parent != source_path.parent:
        raise RuntimeError("imported bundle escapes package directory; upstream may have changed")
    return target


def config_path(config_source):
    """Derive only CONFIG_PATH from the installed paths.ts expressions."""
    source = config_source.read_text()
    literal = r'"(?:[^"\\]|\\.)*"'
    components = unique_match(
        r'const DEFAULT_DIR = path\.join\(os\.homedir\(\), (' + literal +
        r'(?:, ' + literal + r')*)\);', source, "default config directory",
    )
    env_name = unique_match(
        r'const APP_DIR = process\.env\.(\w+) \|\| DEFAULT_DIR;',
        source, "config home expression",
    )
    filename = unique_match(
        r'CONFIG_PATH: path\.join\(APP_DIR, (' + literal + r')\)',
        source, "config path expression",
    )
    default_home = Path.home().joinpath(*json.loads("[" + components + "]"))
    return Path(os.environ.get(env_name) or default_home) / json.loads(filename)


def check_identity_options(path):
    """Read config without invoking upstream getConfig(), which can write it."""
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return
    try:
        config = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        raise RuntimeError("Copilot API config must contain valid JSON") from None
    if not isinstance(config, dict):
        raise RuntimeError("Copilot API config must be a JSON object")
    # Only these options affect this guard; never log values or unrelated config.
    options = {key: config[key] for key in ("modelMappings", "claudeAutoModel") if key in config}
    mappings = options.get("modelMappings", {})
    auto_model = options.get("claudeAutoModel", "")
    if not isinstance(mappings, dict) or not isinstance(auto_model, str):
        raise RuntimeError("modelMappings must be an object and claudeAutoModel must be a string")
    if mappings or auto_model.strip():
        raise RuntimeError(
            "disable cross-model mappings (modelMappings) and claudeAutoModel "
            "before starting this gateway; configuration was not changed"
        )


def patch_identity(server):
    source = server.read_text(encoding="utf-8")
    anchors = [PREFIX + branch + SUFFIX for branch in (LEGACY_BRANCH, BRANCH)]
    new = PREFIX + "\t" + MARKER + "\n" + SUFFIX
    assignments = re.findall(
        r"\banthropicPayload\.model\s*=\s*(?:getSmallModel\b|smallModel\b)", source,
    )
    unique_boundaries = source.count(PREFIX) == 1 and source.count(SUFFIX) == 1
    if (unique_boundaries and source.count(new) == 1
            and source.count(MARKER) == 1 and not assignments):
        print("Copilot identity patch already applied.", file=sys.stderr)
        return
    matches = [anchor for anchor in anchors if source.count(anchor) == 1]
    if (not unique_boundaries or MARKER in source
            or len(matches) != 1 or len(assignments) != 1):
        raise RuntimeError("unexpected implicit model-substitution anchor; upstream may have changed")
    updated = source.replace(matches[0], new)
    # Replace atomically so another startup cannot load a partly-written bundle.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=server.parent,
                                         prefix=".copilot-identity-", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(updated)
        temporary.chmod(server.stat().st_mode)
        os.replace(temporary, server)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    print("Copilot identity patch applied.", file=sys.stderr)


def main():
    executable = shutil.which("copilot-api")
    if not executable:
        raise RuntimeError("copilot-api is missing from PATH; run this wrapper via start-proxy.sh")
    entry = Path(executable).resolve(strict=True)
    package = entry.parent.parent
    manifest = json.loads((package / "package.json").read_text())
    if not isinstance(manifest, dict) or manifest.get("name") != "@jeffreycao/copilot-api":
        raise RuntimeError("PATH executable is not the expected @jeffreycao/copilot-api package")
    if manifest.get("version") != EXPECTED_VERSION:
        raise RuntimeError(f"Copilot API must be the verified version {EXPECTED_VERSION}; refusing to start")
    binaries = manifest.get("bin")
    if not isinstance(binaries, dict) or binaries.get("copilot-api") != "./dist/main.js":
        raise RuntimeError("unexpected Copilot API package executable; upstream may have changed")
    if entry != (package / "dist/main.js").resolve(strict=True):
        raise RuntimeError("PATH executable does not match the Copilot API package manifest")
    start = imported_bundle(entry, "start", dynamic=True)
    server = imported_bundle(start, "server", dynamic=True)
    config = imported_bundle(start, "config")
    check_identity_options(config_path(config))
    patch_identity(server)
    os.execv(str(entry), [str(entry), "start", "--port", "4141"])


if __name__ == "__main__":
    try:
        main()
    except (OSError, UnicodeError, ValueError):
        print("ERROR: Copilot identity startup cannot read/validate the installed source or config.", file=sys.stderr)
        sys.exit(1)
    except RuntimeError as error:
        print(f"ERROR: Copilot identity startup: {error}", file=sys.stderr)
        sys.exit(1)
