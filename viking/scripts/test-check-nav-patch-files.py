#!/usr/bin/env python3
"""Regression cases for check-nav-patch-files.py (IMPR-1413), on a copy of the tree.

Each case copies the real manifests and nav-patch sources into a scratch root, makes
one change, and asserts the checker's verdict. The first failing case is homelab#250:
a Deployment ``items:`` list that leaves out ov_pref_gate_patch.py.
"""

import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
spec = importlib.util.spec_from_file_location(
    "checker", HERE / "check-nav-patch-files.py"
)
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)

PROD_DEPLOYMENT = checker.DEPLOYMENTS[0][0]
TEST_DEPLOYMENT = checker.DEPLOYMENTS[1][0]


class CheckerCases(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        nav = self.root / checker.NAV
        nav.mkdir(parents=True)
        for path in [REPO / checker.NAV / "sitecustomize.py"] + sorted(
            (REPO / checker.NAV).glob("ov_*_patch.py")
        ):
            shutil.copy(path, nav / path.name)
        for rel in (
            checker.CONFIGMAP[0],
            checker.KUSTOMIZATION[0],
            PROD_DEPLOYMENT,
            TEST_DEPLOYMENT,
        ):
            (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(REPO / rel, self.root / rel)

    def tearDown(self):
        self.tmp.cleanup()

    def errors(self):
        return checker.check(self.root)

    def assertFails(self, needle):
        errors = self.errors()
        self.assertTrue(errors, "checker passed")
        self.assertTrue(any(needle in e for e in errors), errors)

    def edit_docs(self, rel, fn):
        path = self.root / rel
        docs = [d for d in yaml.safe_load_all(path.read_text()) if d is not None]
        for doc in docs:
            fn(doc)
        path.write_text(yaml.safe_dump_all(docs, sort_keys=False))

    def nav_volume(self, doc):
        if doc.get("kind") != "Deployment":
            return None
        for vol in doc["spec"]["template"]["spec"]["volumes"]:
            if vol["name"] == checker.VOLUME:
                return vol
        return None

    def test_tree_passes(self):
        self.assertEqual(self.errors(), [])

    def test_homelab_250_items_list_missing_the_gate_fails(self):
        files = [
            f for f in checker.patch_files(self.root) if f != "ov_pref_gate_patch.py"
        ]

        def add_items(doc):
            vol = self.nav_volume(doc)
            if vol:
                vol["configMap"]["items"] = [{"key": f, "path": f} for f in files]

        self.edit_docs(PROD_DEPLOYMENT, add_items)
        self.assertFails("has an items: list")

    def test_complete_items_list_still_fails(self):
        def add_items(doc):
            vol = self.nav_volume(doc)
            if vol:
                vol["configMap"]["items"] = [
                    {"key": f, "path": f} for f in checker.patch_files(self.root)
                ]

        self.edit_docs(TEST_DEPLOYMENT, add_items)
        self.assertFails("has an items: list")

    def test_wrong_configmap_name_fails(self):
        def rename(doc):
            vol = self.nav_volume(doc)
            if vol:
                vol["configMap"]["name"] = "something-else"

        self.edit_docs(PROD_DEPLOYMENT, rename)
        self.assertFails("mounts 'something-else'")

    def test_new_patch_without_regen_fails_everywhere(self):
        (self.root / checker.NAV / "ov_new_patch.py").write_text('"""New."""\n')
        errors = self.errors()
        for rel in (
            checker.CONFIGMAP[0],
            checker.KUSTOMIZATION[0],
            "sitecustomize.py TARGETS",
        ):
            self.assertTrue(
                any(rel in e and "ov_new_patch" in e for e in errors), (rel, errors)
            )

    def test_kustomization_missing_a_file_fails(self):
        def drop(doc):
            gen = doc["configMapGenerator"][0]
            gen["files"].remove("nav-patch/ov_pref_gate_patch.py")

        self.edit_docs(checker.KUSTOMIZATION[0], drop)
        self.assertFails("missing nav-patch/ov_pref_gate_patch.py")

    def test_kustomization_duplicate_fails(self):
        def dup(doc):
            doc["configMapGenerator"][0]["files"].append("nav-patch/sitecustomize.py")

        self.edit_docs(checker.KUSTOMIZATION[0], dup)
        self.assertFails("listed twice nav-patch/sitecustomize.py")

    def test_kustomization_non_patch_file_fails(self):
        def extra(doc):
            doc["configMapGenerator"][0]["files"].append("nav-patch/canary.py")

        self.edit_docs(checker.KUSTOMIZATION[0], extra)
        self.assertFails("not a patch file nav-patch/canary.py")

    def test_patch_edit_without_regen_fails(self):
        path = self.root / checker.NAV / "ov_pref_gate_patch.py"
        path.write_text(path.read_text() + "\n# edit\n")
        self.assertFails("ov_pref_gate_patch.py differs")

    def test_targets_missing_a_module_fails(self):
        path = self.root / checker.NAV / "sitecustomize.py"
        text = path.read_text()
        self.assertIn(
            '"openviking.service.fs_service": ("ov_grep_scope_patch", "apply"),\n', text
        )
        path.write_text(
            text.replace(
                '"openviking.service.fs_service": ("ov_grep_scope_patch", "apply"),\n',
                "",
            )
        )
        self.edit_docs(checker.CONFIGMAP[0], self.resync_loader)
        self.assertFails("TARGETS: missing ov_grep_scope_patch")

    def resync_loader(self, doc):
        doc["data"]["sitecustomize.py"] = (
            self.root / checker.NAV / "sitecustomize.py"
        ).read_text()


if __name__ == "__main__":
    unittest.main()
