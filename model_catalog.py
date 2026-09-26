#!/usr/bin/env python3
"""Build a one-to-one gateway catalog from Copilot's user-visible models.

The output is JSON (also valid YAML), consumed by LiteLLM as config.yaml.
No provider credentials or network requests belong in this module.
"""
import argparse
import json
import re
import sys


HEADERS = {"editor-version": "vscode/1.100.0", "Copilot-Integration-Id": "vscode-chat"}


def token_label(value):
    if value is None:
        return "미제공"
    if value % 1000000 == 0:
        return f"{value // 1000000}M"
    if value % 1000 == 0:
        return f"{value // 1000}K"
    return f"{value:,}"


def token_limit(limits, key, required=False):
    value = limits.get(key)
    if required and value is None:
        raise ValueError(f"missing {key}; cannot use another provider's limit")
    if value is not None and (type(value) is not int or value <= 0):
        raise ValueError(f"invalid {key}: expected a positive integer")
    return value


def build_config(catalog):
    rows = catalog.get("data") if isinstance(catalog, dict) else None
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError("invalid Copilot catalog: expected data array")
    chat = [row for row in rows if (row.get("capabilities") or {}).get("type") == "chat"]
    if len(chat) < 5:
        raise ValueError(f"only {len(chat)} chat models returned; refusing to replace config")

    selected = []
    seen = set()
    for row in chat:
        if row.get("model_picker_enabled") is not True:
            continue
        model_id, name = row.get("id"), row.get("name")
        if not isinstance(model_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", model_id):
            raise ValueError("invalid Copilot model id")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"missing display name for {model_id}")
        if model_id in seen:
            raise ValueError(f"duplicate Copilot model id: {model_id}")
        seen.add(model_id)
        if re.search(r"internal[\s_-]*only", name, re.IGNORECASE):
            continue
        policy = row.get("policy")
        if policy is None:
            policy = {}
        if not isinstance(policy, dict):
            raise ValueError(f"invalid policy for {model_id}")
        if policy.get("state") not in (None, "enabled"):
            continue
        selected.append(row)
    if not selected:
        raise ValueError("no user-visible, policy-enabled models; refusing to replace config")

    entries = []
    aliases = {}
    public_names = {row["id"] if row["id"].startswith("claude-") else f"claude-{row['id']}" for row in selected}
    if len(public_names) != len(selected):
        raise ValueError("Copilot ids collide with gateway model names")

    def add_alias(alias, target):
        if alias == target:
            return
        if alias in public_names or (alias in aliases and aliases[alias]["model"] != target):
            raise ValueError(f"ambiguous model alias: {alias}")
        aliases[alias] = {"model": target, "hidden": True}

    for row in sorted(selected, key=lambda item: item["id"]):
        model_id = row["id"]
        public_id = model_id if model_id.startswith("claude-") else f"claude-{model_id}"
        endpoints = row.get("supported_endpoints") or []
        if not isinstance(endpoints, list):
            raise ValueError(f"invalid supported_endpoints for {model_id}")
        # Legacy small/fast requests also need the hosted WebSearch translator.
        if ((model_id.startswith("claude-") and "/v1/messages" in endpoints)
                or (model_id == "gpt-4o-mini" and "/chat/completions" in endpoints)
                or any(endpoint in endpoints for endpoint in ("/responses", "ws:/responses"))):
            params = {"model": f"anthropic/{model_id}", "api_base": "http://localhost:4141", "api_key": "dummy"}
        elif "/chat/completions" in endpoints:
            params = {"model": f"github_copilot/{model_id}", "extra_headers": HEADERS.copy()}
        else:
            raise ValueError(f"no supported gateway endpoint for {model_id}")

        limits = row["capabilities"].get("limits")
        if not isinstance(limits, dict):
            raise ValueError(f"invalid limits for {model_id}")
        context = token_limit(limits, "max_context_window_tokens", required=True)
        prompt = token_limit(limits, "max_prompt_tokens", required=True)
        output = token_limit(limits, "max_output_tokens", required=True)
        non_streaming = token_limit(limits, "max_non_streaming_output_tokens")
        description = f"Copilot · 컨텍스트 {token_label(context)} · 입력 {token_label(prompt)} · 출력 {token_label(output)}"
        if non_streaming is not None:
            description += f" · 비스트림 {token_label(non_streaming)}"
        if row.get("preview") is True:
            description += " · Preview"
        info = {
            "mode": "chat", "display_name": " ".join(row["name"].split()),
            "description": description, "gateway_provider": "github_copilot",
            "upstream_model_id": model_id, "upstream_vendor": row.get("vendor"),
        }
        for key, value in (("max_context_window_tokens", context), ("max_input_tokens", prompt),
                           ("max_output_tokens", output), ("max_non_streaming_output_tokens", non_streaming)):
            if value is not None:
                info[key] = value
        entries.append({"model_name": public_id, "litellm_params": params, "model_info": info})

        # Only alternate spellings of this exact upstream model. Never redirect
        # a retired model or another family to a newer/different model.
        spellings = {model_id, public_id}
        if model_id.startswith("claude-"):
            spellings.add(model_id.replace(".", "-"))
        for spelling in sorted(spellings):
            add_alias(spelling, public_id)
            if context is not None and context >= 1000000:
                add_alias(f"{spelling}[1m]", public_id)

    return {
        "model_list": entries,
        "router_settings": {"model_group_alias": aliases},
        "litellm_settings": {"drop_params": True},
        "general_settings": {"master_key": "os.environ/LITELLM_MASTER_KEY"},
    }


def print_models(config):
    print("Model\tCopilot ID\tContext\tInput\tOutput\tNon-stream output")
    for row in config["model_list"]:
        info = row["model_info"]
        values = [info["display_name"], info["upstream_model_id"]]
        values.extend(token_label(info.get(key)) for key in (
            "max_context_window_tokens", "max_input_tokens", "max_output_tokens", "max_non_streaming_output_tokens",
        ))
        print("\t".join(values))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="show real model names and Copilot token limits")
    args = parser.parse_args()
    try:
        config = build_config(json.load(sys.stdin))
    except (ValueError, TypeError, AttributeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if args.list:
        print_models(config)
        return 0
    print("# AUTO-GENERATED by refresh-models.sh — do not edit by hand.")
    json.dump(config, sys.stdout, ensure_ascii=False, indent=2)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
