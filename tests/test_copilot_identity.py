"""Offline tests for strict named-model identity in the Copilot API wrapper."""

import importlib.util
import itertools
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


REPO = Path(__file__).resolve().parents[1]
WRAPPER = REPO / "start-copilot-api.py"
PACKAGES = [
    path for path in Path.home().glob(".npm/_npx/*/node_modules/@jeffreycao/copilot-api")
    if json.loads((path / "package.json").read_text()).get("version") == "2.6.15"
]
PACKAGE = max(PACKAGES, key=lambda path: path.stat().st_mtime) if PACKAGES else None
MARKER = "// PATCHED (local): preserve explicitly requested Copilot model"
LEGACY_BRANCH = '''\tif (!state.tokenBasedBilling && !shouldUseClaudeAutoModel) {
\t\tconst tools = anthropicPayload.tools;
\t\tconst noTools = !tools || tools.length === 0;
\t\tif (anthropicBeta && noTools && compactType === 0) anthropicPayload.model = getSmallModel();
\t}
'''
ORIGINAL_BRANCH = '''\tif (!state.tokenBasedBilling && !shouldUseClaudeAutoModel) {
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

SPEC = importlib.util.spec_from_file_location("copilot_startup", WRAPPER)
PATCHER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PATCHER)


def imported_file(source, prefix):
    names = re.findall(r'await import\("(\./' + prefix + r'-[\w-]+\.js)"\)', source.read_text())
    if len(names) != 1:
        raise AssertionError(f"Expected one imported {prefix} bundle in {source.name}")
    return source.parent / names[0]


class CopilotFixture:
    def __init__(self):
        self.temp = tempfile.TemporaryDirectory(prefix="copilot-identity-test-")
        self.home = Path(self.temp.name)
        self.package = self.home / "variable-cache/node_modules/@jeffreycao/copilot-api"
        self.dist = self.package / "dist"
        self.dist.mkdir(parents=True)
        shutil.copyfile(PACKAGE / "package.json", self.package / "package.json")
        original_main = PACKAGE / "dist/main.js"
        original_start = imported_file(original_main, "start")
        original_server = imported_file(original_start, "server")
        config_name = re.findall(r'from "(\./config-[\w-]+\.js)"', original_start.read_text())[0]
        original_config = original_start.parent / config_name
        for source in (original_main, original_start, original_server, original_config):
            shutil.copyfile(source, self.dist / source.name)
        self.config_source = self.dist / original_config.name
        self.main = self.dist / "main.js"
        self.start = self.dist / original_start.name
        self.server = self.dist / original_server.name
        source = self.server.read_text()
        # Tests still start upstream-unpatched after the wrapper was installed.
        source = source.replace("\t" + MARKER + "\n", ORIGINAL_BRANCH)
        self.server.write_text(source)
        self.main.chmod(0o755)
        self.bin = self.home / "variable-cache/node_modules/.bin"
        self.bin.mkdir()
        (self.bin / "copilot-api").symlink_to(self.main)
        self.environment = {
            **os.environ,
            "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
            "HOME": str(self.home),
            "HOST": "127.0.0.1",
            "COPILOT_API_HOME": str(self.home / "api-home"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        self.config = self.home / "api-home/config.json"
        self.config.parent.mkdir()
        self.config.write_text('{"modelMappings":{}}')

    def run(self):
        # Intercept only the process-replacement boundary. The actual wrapper
        # resolves the package, validates and writes its copied source normally.
        code = '''import json, os, runpy, sys
os.execv = lambda executable, argv: print(json.dumps({"exec": executable, "argv": argv, "host": os.environ.get("HOST")}))
sys.argv = [sys.argv[1]]
runpy.run_path(sys.argv[0], run_name="__main__")
'''
        return subprocess.run(
            [sys.executable, "-B", "-c", code, str(WRAPPER)],
            env=self.environment, text=True, capture_output=True, timeout=20,
        )

    def close(self):
        self.temp.cleanup()


def execute_identity_excerpt(source, requested="claude-sonnet-5"):
    """Execute real bundled branch logic, not a Python reimplementation."""
    rows = []
    for billing, beta, tools, compact, auto in itertools.product(
        (False, True), (None, "test-beta"), (None, [], [{"name": "tool"}]),
        (0, 1, 2), (False, True),
    ):
        rows.append({"billing": billing, "beta": beta, "tools": tools,
                     "compact": compact, "auto": auto, "requested": requested})
    javascript = r'''
const fs = require("node:fs");
const source = fs.readFileSync(process.argv[1], "utf8");
const handler = source.indexOf('async function handleCompletionPayload(');
const start = source.indexOf('\tconst anthropicBeta = c.req.header("anthropic-beta");', handler);
const end = source.indexOf('\tif (compactType) logger$9.debug(', start);
if (handler < 0 || start < 0 || end < 0) throw new Error("Missing real model-selection excerpt");
const select = new Function("state", "anthropicPayload", "shouldUseClaudeAutoModel",
  "compactType", "c", "logger$9", "getSmallModel", "consola", source.slice(start, end) +
  '\nreturn anthropicPayload.model;');
const rows = JSON.parse(fs.readFileSync(0, "utf8"));
const results = rows.map(row => {
  const state = {tokenBasedBilling: row.billing};
  let calls = 0;
  const payload = {model: row.requested, tools: row.tools};
  const model = select(state, payload, row.auto, row.compact,
    {req: {header: () => row.beta}}, {debug: () => {}},
    () => { calls++; return "gpt-5-mini"; }, {debug: () => {}});
  return {...row, model, calls, billingAfter: state.tokenBasedBilling};
});
console.log(JSON.stringify(results));
'''
    result = subprocess.run(
        [shutil.which("node"), "-e", javascript, str(source)],
        input=json.dumps(rows), text=True, capture_output=True, timeout=20,
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


class IdentityPatchTests(unittest.TestCase):
    """The safety contract runs even without an npm cache or Node installation."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="copilot-anchor-test-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.server = self.directory / "server.js"

    def assert_rejected(self, source):
        self.server.write_text(source)
        before = self.server.stat()
        with self.assertRaisesRegex(RuntimeError, "upstream"):
            PATCHER.patch_identity(self.server)
        self.assertEqual(self.server.read_text(), source)
        self.assertEqual(self.server.stat().st_ino, before.st_ino)
        self.assertEqual(list(self.directory.glob(".copilot-identity-*")), [])

    def test_known_legacy_and_2615_layouts_are_atomic_and_idempotent(self):
        for branch in (LEGACY_BRANCH, ORIGINAL_BRANCH):
            with self.subTest(layout="2.6.15" if branch == ORIGINAL_BRANCH else "legacy"):
                original = PREFIX + branch + SUFFIX
                expected = PREFIX + "\t" + MARKER + "\n" + SUFFIX
                self.server.write_text(original)
                self.server.chmod(0o640)
                replace = os.replace

                def check_atomic(source, destination):
                    self.assertEqual(destination.read_text(), original)
                    self.assertEqual(source.read_text(), expected)
                    self.assertEqual(stat.S_IMODE(source.stat().st_mode), 0o640)
                    replace(source, destination)

                with mock.patch.object(PATCHER.os, "replace", side_effect=check_atomic) as patched:
                    PATCHER.patch_identity(self.server)
                    patched.assert_called_once()
                before = self.server.stat()
                PATCHER.patch_identity(self.server)
                self.assertEqual(self.server.read_text(), expected)
                self.assertEqual((self.server.stat().st_ino, self.server.stat().st_mtime_ns),
                                 (before.st_ino, before.st_mtime_ns))
                self.assertEqual(list(self.directory.glob(".copilot-identity-*")), [])

    def test_unknown_conditions_loggers_and_substitution_layouts_fail_closed(self):
        for branch in (LEGACY_BRANCH, ORIGINAL_BRANCH):
            original = PREFIX + branch + SUFFIX
            for old, new in (("compactType === 0", "compactType === 2"),
                             ("getSmallModel()", "selectAnotherModel()"),
                             ("!state.tokenBasedBilling", "state.tokenBasedBilling"),
                             ("logger$9", "logger$10")):
                with self.subTest(branch=branch, mutation=old):
                    self.assert_rejected(original.replace(old, new))

    def test_duplicate_and_mixed_anchors_or_assignments_fail_closed(self):
        for branch in (LEGACY_BRANCH, ORIGINAL_BRANCH):
            original = PREFIX + branch + SUFFIX
            for duplicate in (original * 2, PREFIX + branch * 2 + SUFFIX,
                              original + LEGACY_BRANCH, original + ORIGINAL_BRANCH,
                              original + PREFIX, original + SUFFIX,
                              original + "anthropicPayload.model = smallModel;",
                              original + "anthropicPayload.model=getSmallModel();"):
                with self.subTest(source=duplicate):
                    self.assert_rejected(duplicate)

    def test_partial_or_modified_patches_fail_closed(self):
        patched = PREFIX + "\t" + MARKER + "\n" + SUFFIX
        for source in (patched.replace(MARKER, MARKER + " altered"),
                       PREFIX + "\t" + MARKER + "\n",
                       patched + LEGACY_BRANCH, patched + ORIGINAL_BRANCH,
                       patched + "anthropicPayload.model = getSmallModel();",
                       patched + "anthropicPayload.model = smallModel;",
                       patched + PREFIX, patched + SUFFIX, patched * 2):
            with self.subTest(source=source):
                self.assert_rejected(source)

    def test_failed_atomic_replace_keeps_original_and_cleans_temporary_file(self):
        original = PREFIX + ORIGINAL_BRANCH + SUFFIX
        self.server.write_text(original)
        with mock.patch.object(PATCHER.os, "replace", side_effect=OSError("test replace failure")):
            with self.assertRaisesRegex(OSError, "test replace failure"):
                PATCHER.patch_identity(self.server)
        self.assertEqual(self.server.read_text(), original)
        self.assertEqual(list(self.directory.glob(".copilot-identity-*")), [])


@unittest.skipUnless(PACKAGE and shutil.which("node"),
                     "requires cached Copilot API 2.6.15 and Node for offline tests")
class CopilotIdentityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = CopilotFixture()
        self.addCleanup(self.fixture.close)

    def assert_started(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        lines = [line for line in result.stdout.splitlines() if line.startswith('{"exec":')]
        self.assertEqual(len(lines), 1, result.stdout)
        return json.loads(lines[0])

    def test_upstream_excerpt_demonstrates_implicit_small_model_substitution(self):
        results = execute_identity_excerpt(self.fixture.server)
        rewritten = [row for row in results if row["model"] != "claude-sonnet-5"]
        self.assertEqual(len(rewritten), 2)
        for row in rewritten:
            self.assertEqual(row["model"], "gpt-5-mini")
            self.assertFalse(row["billing"])
            self.assertTrue(row["beta"])
            self.assertFalse(row["tools"])
            self.assertEqual(row["compact"], 0)
            self.assertFalse(row["auto"])

    def test_wrapper_preserves_named_model_in_every_billing_tools_beta_combination(self):
        self.assert_started(self.fixture.run())
        for name in ("claude-sonnet-5", "claude-opus-5.5", "claude-haiku-4.5",
                     "gpt-5-mini", "gpt-6-astra", "grok-4.7"):
            for row in execute_identity_excerpt(self.fixture.server, name):
                with self.subTest(row=row):
                    self.assertEqual(row["model"], name)
                    self.assertEqual(row["calls"], 0)
                    self.assertEqual(row["billingAfter"], row["billing"])

    def test_exec_uses_path_resolved_entry_and_original_start_arguments(self):
        started = self.assert_started(self.fixture.run())
        self.assertEqual(started, {
            "exec": str(self.fixture.main),
            "argv": [str(self.fixture.main), "start", "--port", "4141"],
            "host": "127.0.0.1",
        })

    def test_only_implicit_branch_changes_and_second_start_is_idempotent(self):
        before = {path: path.read_bytes() for path in self.fixture.package.rglob("*") if path.is_file()}
        config_before = self.fixture.config.read_bytes()
        self.assert_started(self.fixture.run())
        after = {path: path.read_bytes() for path in before}
        source = before[self.fixture.server].decode()
        expected = source.replace(ORIGINAL_BRANCH, "\t" + MARKER + "\n")
        self.assertTrue(after[self.fixture.server].decode() == expected,
                        "Only the implicit model-substitution branch may change")
        for path in before:
            if path != self.fixture.server:
                self.assertEqual(before[path], after[path], path.name)
        second = self.fixture.run()
        self.assert_started(second)
        self.assertIn("already applied", second.stderr)
        for path in after:
            self.assertEqual(after[path], path.read_bytes(), path.name)
        self.assertEqual(config_before, self.fixture.config.read_bytes())

    def test_missing_executable_fails_without_starting(self):
        self.fixture.environment["PATH"] = str(self.fixture.home / "missing-bin")
        result = self.fixture.run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ERROR", result.stderr)
        self.assertNotIn('"exec":', result.stdout)

    def test_wrong_package_identity_fails_without_writes_or_starting(self):
        manifest = self.fixture.package / "package.json"
        data = json.loads(manifest.read_text())
        data["name"] = "other-package"
        manifest.write_text(json.dumps(data))
        before = self.fixture.server.read_bytes()
        result = self.fixture.run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ERROR", result.stderr)
        self.assertNotIn('"exec":', result.stdout)
        self.assertEqual(before, self.fixture.server.read_bytes())

    def test_changed_or_duplicate_anchor_fails_without_writes_or_starting(self):
        original = self.fixture.server.read_text()
        self.assertEqual(original.count(ORIGINAL_BRANCH), 1)
        for changed in (
            original.replace("compactType === 0", "compactType === 2"),
            original.replace(ORIGINAL_BRANCH, ORIGINAL_BRANCH * 2),
        ):
            with self.subTest(duplicate=changed.count(ORIGINAL_BRANCH) > 1):
                self.fixture.server.write_text(changed)
                result = self.fixture.run()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("upstream", result.stderr)
                self.assertNotIn('"exec":', result.stdout)
                self.assertEqual(changed, self.fixture.server.read_text())

    def test_unverified_package_version_fails_without_writes_or_starting(self):
        manifest = self.fixture.package / "package.json"
        data = json.loads(manifest.read_text())
        data["version"] = "2.6.16"
        manifest.write_text(json.dumps(data))
        before = self.fixture.server.read_bytes()
        result = self.fixture.run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("verified version 2.6.15", result.stderr)
        self.assertNotIn('"exec":', result.stdout)
        self.assertEqual(before, self.fixture.server.read_bytes())

    def test_changed_existing_patch_fails_without_starting(self):
        self.assert_started(self.fixture.run())
        changed = self.fixture.server.read_text().replace(MARKER, MARKER + " altered")
        self.fixture.server.write_text(changed)
        result = self.fixture.run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("upstream", result.stderr)
        self.assertNotIn('"exec":', result.stdout)
        self.assertEqual(changed, self.fixture.server.read_text())

    def test_changed_start_import_fails_without_writes_or_starting(self):
        source = self.fixture.start.read_text().replace(
            "./" + self.fixture.server.name, "./server-unknown.js"
        )
        self.fixture.start.write_text(source)
        before = self.fixture.server.read_bytes()
        result = self.fixture.run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ERROR", result.stderr)
        self.assertNotIn('"exec":', result.stdout)
        self.assertEqual(before, self.fixture.server.read_bytes())

    def test_active_identity_overrides_are_rejected_without_modifying_config(self):
        for options in ({"modelMappings": {"named": "hidden-replacement"}},
                        {"claudeAutoModel": "hidden-replacement"}):
            with self.subTest(options=list(options)):
                self.fixture.config.write_text(json.dumps({**options, "secret": "do-not-print"}))
                before = self.fixture.config.read_bytes()
                server_before = self.fixture.server.read_bytes()
                result = self.fixture.run()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("disable", result.stderr.lower())
                self.assertNotIn('"exec":', result.stdout)
                self.assertNotIn("hidden-replacement", result.stdout + result.stderr)
                self.assertNotIn("do-not-print", result.stdout + result.stderr)
                self.assertEqual(before, self.fixture.config.read_bytes())
                self.assertEqual(server_before, self.fixture.server.read_bytes())

    def test_invalid_identity_option_types_and_json_fail_closed(self):
        values = ("{broken-json-secret", "[]", '{"modelMappings": null}',
                  '{"modelMappings": []}', '{"claudeAutoModel": 123}',
                  '{"claudeAutoModel": null}')
        for raw in values:
            with self.subTest(raw=raw):
                self.fixture.config.write_text(raw)
                result = self.fixture.run()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("ERROR", result.stderr)
                self.assertNotIn('"exec":', result.stdout)
                self.assertNotIn("broken-json-secret", result.stdout + result.stderr)
                self.assertEqual(raw, self.fixture.config.read_text())

    def test_missing_config_and_empty_overrides_allow_start(self):
        self.fixture.config.unlink()
        self.assert_started(self.fixture.run())
        self.assertFalse(self.fixture.config.exists())
        self.fixture.config.write_text('{"modelMappings": {}, "claudeAutoModel": "  "}')
        before = self.fixture.config.read_bytes()
        self.assert_started(self.fixture.run())
        self.assertEqual(before, self.fixture.config.read_bytes())

    def test_explicit_hosted_websearch_config_stays_allowed(self):
        self.fixture.config.write_text(json.dumps({
            "messageApiWebSearchModel": "gpt-5-mini", "alphaSearchModel": "gpt-5-mini",
            "useResponsesApiWebSearch": True, "smallModel": "gpt-5-mini",
        }))
        before = self.fixture.config.read_bytes()
        self.assert_started(self.fixture.run())
        self.assertEqual(before, self.fixture.config.read_bytes())

    def test_config_location_is_derived_from_installed_paths_code(self):
        self.fixture.environment.pop("COPILOT_API_HOME")
        source = self.fixture.config_source.read_text().replace(
            '".local", "share", "copilot-api"', '"custom", "api-home"'
        )
        self.fixture.config_source.write_text(source)
        config = self.fixture.home / "custom/api-home/config.json"
        config.parent.mkdir(parents=True)
        config.write_text('{"claudeAutoModel": "do-not-print"}')
        before = config.read_bytes()
        result = self.fixture.run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("disable", result.stderr.lower())
        self.assertNotIn('"exec":', result.stdout)
        self.assertNotIn("do-not-print", result.stdout + result.stderr)
        self.assertEqual(before, config.read_bytes())

    def test_changed_config_path_expression_fails_closed(self):
        source = self.fixture.config_source.read_text().replace(
            "process.env.COPILOT_API_HOME || DEFAULT_DIR", "getOtherHome()"
        )
        self.fixture.config_source.write_text(source)
        result = self.fixture.run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("upstream", result.stderr)
        self.assertNotIn('"exec":', result.stdout)

    def test_default_config_directory_is_not_modified(self):
        self.fixture.environment.pop("COPILOT_API_HOME")
        config = self.fixture.home / ".local/share/copilot-api/config.json"
        config.parent.mkdir(parents=True)
        config.write_text('{"modelMappings": {}, "privateValue": "do-not-print"}')
        before = config.read_bytes()
        result = self.fixture.run()
        self.assert_started(result)
        self.assertEqual(before, config.read_bytes())
        self.assertNotIn("do-not-print", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
