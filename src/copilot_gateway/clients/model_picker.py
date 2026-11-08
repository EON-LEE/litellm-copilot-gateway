"""Account-scoped model catalogs for clients with independent model pickers."""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import urllib.request

from .. import catalog, services
from ..settings import write_private

CLAUDE_MIN_VERSION = (2, 1, 318)
CODEX_MIN_VERSION = (0, 167, 0)
MAX_CATALOG_BYTES = 16 * 1024 * 1024


def require_version(executable, name, minimum):
    try:
        result = subprocess.run([executable, "--version"], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=10, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        raise services.ServiceError(f"cannot check {name} version: {error}") from None
    match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", result.stdout)
    if result.returncode or not match or tuple(map(int, match.groups())) < minimum:
        required = ".".join(map(str, minimum))
        raise services.ServiceError(f"model picker synchronization requires {name} {required} or newer")


def claude_picker(config):
    return {
        "replaceBuiltInOptions": True,
        "options": [
            {"model": row["model_name"], "label": row["model_info"]["display_name"],
             "description": row["model_info"]["description"]}
            for row in config["model_list"]
        ],
    }


def claude_settings_args(argv, config, destination):
    """Merge the last --settings value, as Claude does, without exposing file contents in argv."""
    remaining = []
    supplied = None
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--":
            remaining.extend(argv[index:])
            break
        if arg == "--settings":
            index += 1
            if index >= len(argv) or argv[index] == "--":
                raise services.ServiceError("--settings requires a JSON object or settings file")
            supplied = argv[index]
        elif arg.startswith("--settings="):
            supplied = arg.partition("=")[2]
        else:
            remaining.append(arg)
        index += 1

    settings = {}
    if supplied is not None:
        if not supplied:
            raise services.ServiceError("--settings requires a JSON object or settings file")
        try:
            content = (supplied if supplied.lstrip().startswith(("{", "["))
                       else Path(supplied).expanduser().read_text(encoding="utf-8"))
            settings = json.loads(content)
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise services.ServiceError(f"cannot read Claude --settings: {error}") from None
        if not isinstance(settings, dict):
            raise services.ServiceError("Claude --settings must contain a JSON object")
    settings["modelPicker"] = claude_picker(config)
    destination = Path(destination)
    write_private(destination, json.dumps(settings, ensure_ascii=False))
    return ["--settings", str(destination), *remaining]


def _read_capi(settings):
    request = urllib.request.Request(f"{settings.capi_base}/v1/models")
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=10) as response:
            body = response.read(MAX_CATALOG_BYTES + 1)
        if len(body) > MAX_CATALOG_BYTES:
            raise services.ServiceError("copilot-api model catalog exceeds the size limit")
        return json.loads(body)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise services.ServiceError(f"cannot read copilot-api model catalog: {error}") from None


def _index_models(payload, array_name, id_name):
    rows = payload.get(array_name) if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise services.ServiceError(f"invalid copilot-api model catalog: missing {array_name}")
    result = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get(id_name), str) or not row[id_name]:
            raise services.ServiceError(f"invalid copilot-api model catalog: missing {id_name}")
        if row[id_name] in result:
            raise services.ServiceError(f"duplicate copilot-api model: {row[id_name]}")
        result[row[id_name]] = row
    return result


def codex_picker(config, copilot_payload, bundled_payload, default=None):
    """Build app-server picker entries without changing Codex's model execution metadata."""
    copilot = _index_models(copilot_payload, "data", "id")
    bundled = _index_models(bundled_payload, "models", "slug")
    aliases = config["router_settings"]["model_group_alias"]
    default = aliases.get(default, {}).get("model", default)
    served = {row["model_name"] for row in config["model_list"]}
    if default not in served:
        default = catalog.codex_default(config)
    selected = []
    for row in config["model_list"]:
        model_id, info = row["model_name"], row["model_info"]
        # copilot-api exposes Claude IDs with the same model's dots normalized to hyphens.
        lookup = model_id.replace(".", "-") if model_id.startswith("claude-") else model_id
        raw = copilot.get(lookup)
        if raw is None:
            raise services.ServiceError(
                f"copilot-api has no metadata for {model_id}; refresh/restart copilot-api and retry")
        policy = raw.get("policy")
        if (raw.get("model_picker_enabled") is not True
                or (policy is not None and (not isinstance(policy, dict)
                                            or policy.get("state") not in (None, "enabled")))):
            raise services.ServiceError(f"copilot-api has a stale or disabled model entry for {model_id}")
        capabilities = raw.get("capabilities")
        support = capabilities.get("supports") if isinstance(capabilities, dict) else None
        if not isinstance(support, dict):
            raise services.ServiceError(f"copilot-api has no capability metadata for {model_id}")
        effort = support.get("reasoning_effort", [])
        if not isinstance(effort, list) or not all(isinstance(value, str) for value in effort):
            raise services.ServiceError(f"invalid reasoning capability for {model_id}")
        original = bundled.get(model_id)
        if original:
            levels = original.get("supported_reasoning_levels")
            if not isinstance(levels, list) or not all(isinstance(level, dict) for level in levels):
                raise services.ServiceError(f"invalid Codex reasoning metadata for {model_id}")
            levels = [{"reasoningEffort": level["effort"], "description": level.get("description", "")}
                      for level in levels if level.get("effort") in effort]
            default_effort = original.get("default_reasoning_level")
        else:
            levels = [{"reasoningEffort": level, "description": f"{level.capitalize()} reasoning"}
                      for level in effort]
            default_effort = None
        available_efforts = [level["reasoningEffort"] for level in levels]
        if default_effort not in available_efforts:
            default_effort = ("medium" if "medium" in available_efforts
                              else available_efforts[0] if available_efforts else "medium")
        selected.append({
            "id": model_id,
            "model": model_id,
            "displayName": info["display_name"],
            "description": info["description"],
            "hidden": False,
            "isDefault": model_id == default,
            "inputModalities": ["text", "image"] if support.get("vision") is True else ["text"],
            "supportedReasoningEfforts": levels,
            "defaultReasoningEffort": default_effort,
            "additionalSpeedTiers": [],
            "serviceTiers": [],
            "upgrade": None,
            "upgradeInfo": None,
        })
    if not selected:
        raise services.ServiceError("no Copilot models are available for the Codex picker")
    return selected


def _read_bundled_codex(executable, env):
    try:
        result = subprocess.run([executable, "debug", "models", "--bundled"], env=env,
                                capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        raise services.ServiceError(f"cannot read the bundled Codex model catalog: {error}") from None
    if result.returncode or len(result.stdout) > MAX_CATALOG_BYTES:
        raise services.ServiceError("cannot read the bundled Codex model catalog")
    try:
        return json.loads(result.stdout)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise services.ServiceError(f"invalid bundled Codex model catalog: {error}") from None


def load_codex_picker(settings, config, executable, env, default=None):
    return codex_picker(config, _read_capi(settings), _read_bundled_codex(executable, env), default)
