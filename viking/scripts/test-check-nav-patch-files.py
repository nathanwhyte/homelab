#!/usr/bin/env python3
"""Regression cases for check-nav-patch-files.py (IMPR-1413), on a copy of the tree.

Each case copies viking/manifests/ into a scratch root, makes one change, and asserts
the checker's verdict; the checker renders the copy with ``kubectl kustomize``. The
first failing case is homelab#250: a Deployment ``items:`` list that leaves out
ov_pref_gate_patch.py. The rest are the review findings on homelab#254.
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

CONFIGMAP = "viking/manifests/openviking-nav-patch-configmap.yaml"
KUSTOMIZATION = "viking/manifests/test/kustomization.yaml"
PROD_DEPLOYMENT = "viking/manifests/openviking-deployment.yaml"
TEST_DEPLOYMENT = "viking/manifests/test/openviking-test-deployment.yaml"
GREP_LINE = '    "openviking.service.fs_service": ("ov_grep_scope_patch", "apply"),\n'


class CheckerCases(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        shutil.copytree(
            REPO / "viking" / "manifests",
            self.root / "viking" / "manifests",
            ignore=shutil.ignore_patterns("__pycache__"),
        )

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

    def append(self, rel, text):
        path = self.root / rel
        path.write_text(path.read_text() + text)

    @staticmethod
    def pod_spec(doc):
        if doc.get("kind") != "Deployment":
            return None
        return doc["spec"]["template"]["spec"]

    def nav_volume(self, doc):
        spec = self.pod_spec(doc)
        for vol in (spec or {}).get("volumes", []):
            if vol["name"] == checker.VOLUME:
                return vol
        return None

    def edit_loader(self, old, new):
        path = self.root / checker.NAV / "sitecustomize.py"
        text = path.read_text()
        self.assertIn(old, text)
        path.write_text(text.replace(old, new))
        self.edit_docs(CONFIGMAP, self.resync_loader)

    def resync_loader(self, doc):
        doc["data"]["sitecustomize.py"] = (
            self.root / checker.NAV / "sitecustomize.py"
        ).read_text()

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
        self.assertFails(
            "expected one volume nav-patch of ConfigMap openviking-nav-patch"
        )

    def test_new_patch_without_regen_fails_everywhere(self):
        (self.root / checker.NAV / "ov_new_patch.py").write_text('"""New."""\n')
        errors = self.errors()
        for where in (
            "viking/manifests (kustomize)",
            "viking/manifests/test (kustomize)",
            "TARGETS",
        ):
            self.assertTrue(
                any(where in e and "ov_new_patch" in e for e in errors), (where, errors)
            )

    def test_kustomization_missing_a_file_fails(self):
        def drop(doc):
            doc["configMapGenerator"][0]["files"].remove(
                "nav-patch/ov_pref_gate_patch.py"
            )

        self.edit_docs(KUSTOMIZATION, drop)
        self.assertFails("missing ov_pref_gate_patch.py")

    def test_kustomization_non_patch_file_fails(self):
        def extra(doc):
            doc["configMapGenerator"][0]["files"].append("nav-patch/canary.py")

        self.edit_docs(KUSTOMIZATION, extra)
        self.assertFails("not a patch file canary.py")

    def test_generator_literal_fails(self):
        # R1: a literal adds a mounted key outside `files`.
        def literal(doc):
            doc["configMapGenerator"][0]["literals"] = ["openviking.py=x"]

        self.edit_docs(KUSTOMIZATION, literal)
        self.assertFails("not a patch file openviking.py")

    def test_kustomize_patch_on_the_configmap_fails(self):
        # R2: a top-level patch rewrites the generated ConfigMap.
        def patch(doc):
            doc["patches"] = [
                {
                    "target": {"kind": "ConfigMap", "name": "ov-nav-patch"},
                    "patch": "- op: add\n  path: /data/openviking.py\n  value: x\n",
                }
            ]

        self.edit_docs(KUSTOMIZATION, patch)
        self.assertFails("not a patch file openviking.py")

    def test_list_wrapped_second_configmap_fails(self):
        # R2: a List resource defining the ConfigMap again.
        self.append(
            CONFIGMAP,
            "---\napiVersion: v1\nkind: List\nitems:\n"
            "  - apiVersion: v1\n    kind: ConfigMap\n    metadata:\n"
            "      name: openviking-nav-patch\n      namespace: viking\n"
            "    data:\n      sitecustomize.py: ''\n",
        )
        self.assertFails("viking/manifests (kustomize)")

    def test_missing_volume_mount_fails(self):
        # R2: the volume is declared but no container mounts it.
        def unmount(doc):
            spec = self.pod_spec(doc)
            for c in (spec or {}).get("containers", []):
                c["volumeMounts"] = [
                    m for m in c.get("volumeMounts", []) if m["name"] != checker.VOLUME
                ]

        self.edit_docs(TEST_DEPLOYMENT, unmount)
        self.assertFails("container openviking-test does not mount nav-patch")

    def test_subpath_mount_fails(self):
        def subpath(doc):
            spec = self.pod_spec(doc)
            for c in (spec or {}).get("containers", []):
                for m in c.get("volumeMounts", []):
                    if m["name"] == checker.VOLUME:
                        m["subPath"] = "sitecustomize.py"

        self.edit_docs(PROD_DEPLOYMENT, subpath)
        self.assertFails("with a subPath")

    def test_overlapping_mount_fails(self):
        def overlap(doc):
            spec = self.pod_spec(doc)
            for c in (spec or {}).get("containers", []):
                if any(m["name"] == checker.VOLUME for m in c.get("volumeMounts", [])):
                    c["volumeMounts"].append(
                        {"name": "data", "mountPath": "/ov-nav-patch/ov_nav_patch.py"}
                    )

        self.edit_docs(PROD_DEPLOYMENT, overlap)
        self.assertFails("hides nav-patch files")

    def test_mount_off_pythonpath_fails(self):
        def move(doc):
            spec = self.pod_spec(doc)
            for c in (spec or {}).get("containers", []):
                for m in c.get("volumeMounts", []):
                    if m["name"] == checker.VOLUME:
                        m["mountPath"] = "/elsewhere"

        self.edit_docs(TEST_DEPLOYMENT, move)
        self.assertFails("but its PYTHONPATH is")

    def test_mount_only_in_an_init_container_fails(self):
        # R3: an init container's mount does not put the patches in the app.
        def move_to_init(doc):
            spec = self.pod_spec(doc)
            if not spec:
                return
            app = spec["containers"][0]
            mount = next(m for m in app["volumeMounts"] if m["name"] == checker.VOLUME)
            app["volumeMounts"].remove(mount)
            env = [e for e in app["env"] if e["name"] == "PYTHONPATH"]
            spec["initContainers"] = [
                {
                    "name": "init",
                    "image": "busybox",
                    "env": env,
                    "volumeMounts": [mount],
                }
            ]

        self.edit_docs(TEST_DEPLOYMENT, move_to_init)
        self.assertFails("container openviking-test does not mount nav-patch")

    def test_items_hidden_from_kustomize_but_in_the_source_file_fails(self):
        # R3: a single-file apply still sends the items: list a kustomize patch drops.
        files = [
            f for f in checker.patch_files(self.root) if f != "ov_pref_gate_patch.py"
        ]

        def add_items(doc):
            vol = self.nav_volume(doc)
            if vol:
                vol["configMap"]["items"] = [{"key": f, "path": f} for f in files]

        self.edit_docs(PROD_DEPLOYMENT, add_items)
        index = self.find_volume_index(PROD_DEPLOYMENT)
        self.edit_docs(
            "viking/manifests/kustomization.yaml",
            lambda doc: doc.setdefault("patches", []).append(
                {
                    "target": {"kind": "Deployment", "name": "openviking"},
                    "patch": "- op: remove\n"
                    f"  path: /spec/template/spec/volumes/{index}/configMap/items\n",
                }
            ),
        )
        errors = self.errors()
        self.assertFalse(
            any("(kustomize)" in e and "items:" in e for e in errors), errors
        )
        self.assertTrue(
            any("(source files)" in e and "items:" in e for e in errors), errors
        )

    def find_volume_index(self, rel):
        for doc in yaml.safe_load_all((self.root / rel).read_text()):
            spec = self.pod_spec(doc or {})
            if spec:
                return next(
                    i
                    for i, v in enumerate(spec["volumes"])
                    if v["name"] == checker.VOLUME
                )
        raise AssertionError("no nav-patch volume")

    def test_another_workload_with_an_items_list_fails(self):
        # R3: a second workload consuming the ConfigMap is checked too.
        self.append(
            "viking/manifests/openviking-deployment.yaml",
            "---\napiVersion: apps/v1\nkind: Deployment\nmetadata:\n"
            "  name: canary\n  namespace: viking\nspec:\n"
            "  selector:\n    matchLabels: {app: canary}\n  template:\n"
            "    metadata:\n      labels: {app: canary}\n    spec:\n"
            "      containers:\n        - name: c\n          image: x\n"
            "      volumes:\n        - name: nav-patch\n          configMap:\n"
            "            name: openviking-nav-patch\n"
            "            items: [{key: sitecustomize.py, path: sitecustomize.py}]\n",
        )
        self.assertFails("Deployment canary: volume nav-patch has an items: list")

    def test_projected_volume_of_the_configmap_fails(self):
        def project(doc):
            spec = self.pod_spec(doc)
            if spec:
                spec["volumes"].append(
                    {
                        "name": "other",
                        "projected": {
                            "sources": [{"configMap": {"name": "ov-nav-patch"}}]
                        },
                    }
                )

        self.edit_docs(TEST_DEPLOYMENT, project)
        self.assertFails("volume other uses ov-nav-patch")

    def test_earlier_pythonpath_entry_fails(self):
        # R3: an earlier PYTHONPATH entry can supply a different sitecustomize.py.
        def prepend(doc):
            spec = self.pod_spec(doc)
            for c in (spec or {}).get("containers", []):
                for e in c.get("env", []):
                    if e["name"] == "PYTHONPATH":
                        e["value"] = "/extra-python:" + e["value"]

        self.edit_docs(PROD_DEPLOYMENT, prepend)
        self.assertFails("must be its first entry")

    def test_patch_edit_without_regen_fails(self):
        path = self.root / checker.NAV / "ov_pref_gate_patch.py"
        path.write_text(path.read_text() + "\n# edit\n")
        self.assertFails("ov_pref_gate_patch.py differs")

    def test_targets_missing_a_module_fails(self):
        self.edit_loader(GREP_LINE, "")
        self.assertFails("TARGETS: missing ov_grep_scope_patch")

    def test_duplicate_targets_key_fails(self):
        # R1: a repeated key drops the earlier entry at runtime.
        self.edit_loader(
            GREP_LINE,
            GREP_LINE
            + '    "openviking.service.fs_service": ("ov_nav_patch", "apply"),\n',
        )
        self.assertFails("listed twice openviking.service.fs_service")


if __name__ == "__main__":
    unittest.main()
