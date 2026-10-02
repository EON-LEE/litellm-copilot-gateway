"""copilot-api model-identity guard (synthetic bundles; the real pinned bundle when installed)."""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from copilot_gateway import copilot_api as capi
from copilot_gateway.settings import data_dir


def bundle(branch=capi.BRANCH):
    return "// head\n" + capi.PREFIX + branch + capi.SUFFIX + "// tail\n"


class IdentityPatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="capi-test-")
        self.addCleanup(self.temp.cleanup)
        self.server = Path(self.temp.name) / "server.js"

    def write(self, text):
        self.server.write_text(text, encoding="utf-8", newline="")

    def test_both_known_upstream_shapes_patch_once(self):
        for branch in (capi.BRANCH, capi.LEGACY_BRANCH):
            with self.subTest(legacy=branch is capi.LEGACY_BRANCH):
                self.write(bundle(branch))
                self.assertTrue(capi.patch_identity(self.server))
                patched = self.server.read_text(encoding="utf-8")
                self.assertIn(capi.MARKER, patched)
                self.assertNotIn("getSmallModel()", patched)
                self.assertFalse(capi.patch_identity(self.server))
                self.assertEqual(self.server.read_text(encoding="utf-8"), patched)

    def test_unknown_or_ambiguous_bundles_fail_without_writing(self):
        changed = bundle().replace("compactType === 0", "compactType == 0")
        for text in (changed, bundle() + capi.PREFIX, "// nothing\n",
                     bundle() + "anthropicPayload.model = getSmallModel();\n"):
            with self.subTest(text=text[-60:]):
                self.write(text)
                with self.assertRaises(capi.CopilotApiError):
                    capi.patch_identity(self.server)
                self.assertEqual(self.server.read_text(encoding="utf-8"), text)

    def test_cross_model_config_is_refused_without_changes(self):
        path = Path(self.temp.name) / "config.json"
        capi.check_identity_options(path)  # missing is fine
        for good in ("", "{}", '{"modelMappings": {}, "claudeAutoModel": ""}'):
            path.write_text(good, encoding="utf-8")
            capi.check_identity_options(path)
        for bad in ('{"modelMappings": {"a": "b"}}', '{"claudeAutoModel": "x"}', "[]", "{", '{"modelMappings": 1}'):
            with self.subTest(bad=bad):
                path.write_text(bad, encoding="utf-8")
                with self.assertRaises(capi.CopilotApiError):
                    capi.check_identity_options(path)
                self.assertEqual(path.read_text(encoding="utf-8"), bad)


def installed_package():
    home = os.environ.get("CCGW_TEST_HOME") or str(data_dir())
    package = capi.package_dir(home)
    return package if (package / "package.json").exists() else None


@unittest.skipUnless(installed_package(), "pinned copilot-api not installed (run `ccgw setup`)")
class InstalledBundleTests(unittest.TestCase):
    def test_installed_bundle_is_pinned_and_patched(self):
        package = installed_package()
        self.assertEqual(json.loads((package / "package.json").read_text(encoding="utf-8"))["version"],
                         capi.EXPECTED_VERSION)
        with tempfile.TemporaryDirectory() as temp:
            copy = Path(temp) / "pkg"
            shutil.copytree(package / "dist", copy / "dist")
            shutil.copy(package / "package.json", copy / "package.json")
            _, server, _ = capi.verify_package(copy)
            capi.patch_identity(server)
            self.assertIn(capi.MARKER, server.read_text(encoding="utf-8"))
            self.assertFalse(capi.patch_identity(server))


if __name__ == "__main__":
    unittest.main()
