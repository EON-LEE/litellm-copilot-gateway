"""Offline regression tests for the local LiteLLM discovery patch.

Run with: python3 -m unittest discover -s tests -p test_discovery_metadata.py -v
The real Router probe uses the installed uv tool's Python, never its writable source.
"""

import ast
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[1]
INSTALL = Path.home() / ".local/share/uv/tools/litellm"
SITE = INSTALL / "lib/python3.13/site-packages"
PYTHON = INSTALL / "bin/python"
PATCH_MARKER = "# PATCHED (local): expose configured Copilot discovery metadata"
RETURN_ANCHOR = "    if not include_metadata:\n        return base\n"
PATCH_FILES = (
    "litellm/llms/github_copilot/authenticator.py",
    "litellm/llms/anthropic/experimental_pass_through/adapters/transformation.py",
    "litellm/llms/anthropic/experimental_pass_through/adapters/streaming_iterator.py",
    "litellm/proxy/utils.py",
)


class PatchFixture:
    """Copy the installed sources; all installer writes go to temporary HOME."""

    def __init__(self):
        self.temp = tempfile.TemporaryDirectory(prefix="copilot-discovery-test-")
        self.home = Path(self.temp.name)
        self.site = self.home / ".local/share/uv/tools/litellm/lib/python3.13/site-packages"
        for relative in PATCH_FILES:
            destination = self.site / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(SITE / relative, destination)
        self.utils = self.site / PATCH_FILES[-1]
        # Start unpatched even after the user has installed patch 4. This prevents
        # a patched live install from masking a regression in apply-patches.sh.
        source = self.utils.read_text()
        if PATCH_MARKER in source:
            start = source.index("    " + PATCH_MARKER)
            end = source.index(RETURN_ANCHOR, start)
            source = source[:start] + source[end:]
            self.utils.write_text(source)

    def run(self):
        return subprocess.run(
            ["bash", str(REPO / "apply-patches.sh")],
            env={**os.environ, "HOME": str(self.home), "PYTHONDONTWRITEBYTECODE": "1"},
            text=True,
            capture_output=True,
            timeout=30,
        )

    def close(self):
        self.temp.cleanup()


def router_probe(source_path):
    """Execute the patched function with real LiteLLM globals and real Router."""
    import socket

    def no_network(*args, **kwargs):
        raise AssertionError("Network access forbidden in discovery metadata tests")

    socket.socket.connect = no_network
    socket.socket.connect_ex = no_network
    socket.create_connection = no_network
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    from litellm import Router
    import litellm.proxy.utils as utils

    tree = ast.parse(Path(source_path).read_text())
    function = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "create_model_info_response"
    )
    namespace = dict(vars(utils))
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source_path), "exec"), namespace)
    create_response = namespace["create_model_info_response"]

    def deployment(name, info=None, **params):
        entry = {
            "model_name": name,
            "litellm_params": {
                "model": "openai/gpt-4o-mini",
                "api_base": "http://127.0.0.1:4141",
                "api_key": "offline-test-not-a-secret",
                **params,
            },
        }
        if info is not None:
            entry["model_info"] = {
                # Explicit cache pricing avoids unrelated Router registration warnings.
                "cache_creation_input_token_cost": 0,
                "cache_read_input_token_cost": 0,
                **info,
            }
        return entry

    full_info = {
        "display_name": "Copilot Test Model",
        "description": "Copilot context 128,000; input 120,000; output 8,000 tokens",
        "gateway_provider": "github_copilot",
        "upstream_model_id": "test-model",
        "max_context_window_tokens": 128000,
        "max_non_streaming_output_tokens": 4000,
        "max_input_tokens": 120000,
        "max_output_tokens": 8000,
        # These must never overwrite public discovery or leak through wholesale.
        "id": "private-deployment-id",
        "owned_by": "forged-owner",
        "object": "forged-object",
        "created": 0,
        "api_key": "private-config-secret",
        "metadata": {"private": True},
    }
    router = Router(
        model_list=[
            deployment("public-test-model", full_info),
            deployment("second-test-model", {
                "gateway_provider": "github_copilot",
                "display_name": "Second Copilot Model",
                "upstream_model_id": "second-model",
                "max_context_window_tokens": 64000,
            }),
            deployment("optional-test-model", {
                "gateway_provider": "github_copilot",
                "display_name": None,
                "description": None,
                "upstream_model_id": None,
                "max_context_window_tokens": None,
                "max_non_streaming_output_tokens": None,
            }),
            deployment("legacy-test-model"),
            deployment("other-provider-model", {
                "gateway_provider": "other_provider",
                "display_name": "Do not export",
                "description": "Not Copilot",
                "max_context_window_tokens": 999,
            }),
            deployment("params-only-model", None,
                       display_name="Do not export params", description="Not model_info"),
        ],
        fallbacks=[{"public-test-model": ["second-test-model"]}],
    )
    # Cost-map fields are intentionally hostile: only configured model_info may
    # supply discovery text/context, while LiteLLM's standard limits/mode remain.
    cost_info = {
        "mode": "chat",
        "max_input_tokens": 1000,
        "max_output_tokens": 500,
        "display_name": "Do not export cost map",
        "description": "Not configured",
        "gateway_provider": "github_copilot",
        "upstream_model_id": "not-configured",
        "max_context_window_tokens": 999999,
        "max_non_streaming_output_tokens": 999999,
    }

    def response(model, **kwargs):
        return create_response(
            model, "openai", get_model_info=lambda _: cost_info, **kwargs
        )

    results = {
        name: response(name, llm_router=router)
        for name in router.get_model_names()
    }
    results["unknown-model"] = response("unknown-model", llm_router=router)
    results["no-router"] = response("public-test-model")
    results["with-fallbacks"] = response(
        "public-test-model", llm_router=router, include_metadata=True
    )
    try:
        response("public-test-model", llm_router=router,
                 include_metadata=True, fallback_type="invalid")
    except utils.HTTPException as error:
        results["invalid-fallback-status"] = error.status_code
    print(json.dumps(results))


@unittest.skipUnless(PYTHON.exists() and (SITE / PATCH_FILES[-1]).exists(),
                     "requires the installed LiteLLM uv tool for offline integration")
class DiscoveryMetadataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture = PatchFixture()
        cls.addClassCleanup(fixture.close)
        result = fixture.run()
        if result.returncode != 0:
            raise AssertionError(result.stdout + result.stderr)
        probe = subprocess.run(
            [str(PYTHON), "-B", str(Path(__file__).resolve()), "--probe", str(fixture.utils)],
            env={**os.environ, "LITELLM_LOCAL_MODEL_COST_MAP": "True",
                 "PYTHONDONTWRITEBYTECODE": "1"},
            text=True, capture_output=True, timeout=60,
        )
        if probe.returncode != 0:
            raise AssertionError(probe.stdout + probe.stderr)
        cls.responses = json.loads(probe.stdout)

    def test_real_router_exposes_configured_display_and_description(self):
        result = self.responses["public-test-model"]
        self.assertEqual(result.get("display_name"), "Copilot Test Model")
        self.assertEqual(result.get("description"),
                         "Copilot context 128,000; input 120,000; output 8,000 tokens")
        self.assertEqual(result["owned_by"], "github_copilot")
        self.assertEqual(result.get("upstream_model_id"), "test-model")

    def test_real_router_keeps_context_distinct_from_input_and_output(self):
        result = self.responses["public-test-model"]
        self.assertEqual(result.get("max_context_window_tokens"), 128000)
        self.assertEqual(result.get("max_non_streaming_output_tokens"), 4000)
        self.assertEqual(result["max_input_tokens"], 120000)
        self.assertEqual(result["max_output_tokens"], 8000)

    def test_metadata_is_selected_by_public_model_name(self):
        result = self.responses["second-test-model"]
        self.assertEqual(result.get("display_name"), "Second Copilot Model")
        self.assertEqual(result.get("upstream_model_id"), "second-model")
        self.assertEqual(result.get("max_context_window_tokens"), 64000)
        self.assertNotIn("description", result)
        self.assertNotIn("max_non_streaming_output_tokens", result)

    def test_configured_metadata_is_strictly_allowlisted(self):
        result = self.responses["public-test-model"]
        self.assertEqual(result["id"], "public-test-model")
        self.assertEqual(result["object"], "model")
        self.assertEqual(result["created"], 1677610602)
        self.assertEqual(result["mode"], "chat")
        self.assertNotIn("api_key", result)
        self.assertNotIn("gateway_provider", result)
        self.assertNotIn("metadata", result)
        self.assertNotIn("private-config-secret", json.dumps(result))

    def test_absent_or_none_fields_are_not_injected(self):
        result = self.responses["optional-test-model"]
        self.assertEqual(result["owned_by"], "github_copilot")
        for key in ("display_name", "description", "upstream_model_id",
                    "max_context_window_tokens", "max_non_streaming_output_tokens"):
            self.assertNotIn(key, result)

    def test_other_models_and_unconfigured_sources_are_unchanged(self):
        for name in ("legacy-test-model", "other-provider-model", "params-only-model",
                     "unknown-model", "no-router"):
            with self.subTest(name=name):
                self.assertEqual(self.responses[name], {
                    "id": "public-test-model" if name == "no-router" else name,
                    "object": "model", "created": 1677610602, "owned_by": "openai",
                    "mode": "chat", "max_input_tokens": 1000, "max_output_tokens": 500,
                })

    def test_fallback_metadata_is_preserved_alongside_discovery_fields(self):
        result = self.responses["with-fallbacks"]
        self.assertEqual(result.get("display_name"), "Copilot Test Model")
        self.assertEqual(result["metadata"], {"fallbacks": ["second-test-model"]})
        self.assertEqual(self.responses["invalid-fallback-status"], 400)


@unittest.skipUnless((SITE / PATCH_FILES[-1]).exists(), "requires installed LiteLLM source")
class PatchInstallerTests(unittest.TestCase):
    def setUp(self):
        self.fixture = PatchFixture()
        self.addCleanup(self.fixture.close)

    def test_installer_changes_only_discovery_source_then_is_idempotent(self):
        before = {name: (self.fixture.site / name).read_bytes() for name in PATCH_FILES}
        first = self.fixture.run()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        after = {name: (self.fixture.site / name).read_bytes() for name in PATCH_FILES}
        self.assertTrue(before[PATCH_FILES[-1]] != after[PATCH_FILES[-1]],
                        "Patch 4 must change the unpatched discovery function")
        for name in PATCH_FILES[:-1]:
            self.assertEqual(before[name], after[name], name)
        ast.parse(self.fixture.utils.read_text())
        second = self.fixture.run()
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertIn("Patch 4 already applied", second.stdout)
        for name in PATCH_FILES:
            self.assertEqual(after[name], (self.fixture.site / name).read_bytes(), name)

    def test_missing_discovery_source_fails_closed(self):
        self.fixture.utils.unlink()
        result = self.fixture.run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ERROR", result.stderr)
        self.assertIn("utils.py", result.stderr)

    def test_changed_anchor_fails_closed_without_writing_source(self):
        source = self.fixture.utils.read_text().replace(
            RETURN_ANCHOR, "    if include_metadata is False:\n        return base\n"
        )
        self.fixture.utils.write_text(source)
        result = self.fixture.run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Patch 4", result.stderr)
        self.assertIn("upstream", result.stderr)
        self.assertEqual(self.fixture.utils.read_text(), source)

    def test_duplicate_anchor_fails_closed_without_writing_source(self):
        source = self.fixture.utils.read_text().replace(RETURN_ANCHOR, RETURN_ANCHOR * 2)
        self.fixture.utils.write_text(source)
        result = self.fixture.run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Patch 4", result.stderr)
        self.assertEqual(self.fixture.utils.read_text(), source)

    def test_renamed_function_fails_closed_without_writing_source(self):
        source = self.fixture.utils.read_text().replace(
            "def create_model_info_response(", "def replaced_model_info_response("
        )
        self.fixture.utils.write_text(source)
        result = self.fixture.run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Patch 4", result.stderr)
        self.assertEqual(self.fixture.utils.read_text(), source)

    def test_modified_existing_patch_is_not_silently_accepted(self):
        first = self.fixture.run()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        source = self.fixture.utils.read_text().replace(PATCH_MARKER, PATCH_MARKER + " changed")
        self.fixture.utils.write_text(source)
        result = self.fixture.run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Patch 4", result.stderr)
        self.assertEqual(self.fixture.utils.read_text(), source)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--probe":
        router_probe(sys.argv[2])
    else:
        unittest.main()
