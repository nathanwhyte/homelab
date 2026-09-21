"""Post-import hook that installs the IMPR-1185 navigation patch.

Mounted from a ConfigMap onto PYTHONPATH. Python's ``site`` imports this file at
interpreter start in every process, including the uvicorn workers. It patches
``openviking.storage.queuefs.semantic_processor`` only in a process that imports
it, so the ``ov`` CLI and the entrypoint's helper interpreters are unaffected.
Rollback: remove PYTHONPATH from the Deployment (``kubectl set env … PYTHONPATH-``).
"""

import importlib.abc
import importlib.machinery
import sys

TARGET = "openviking.storage.queuefs.semantic_processor"


class _PatchingLoader(importlib.abc.Loader):
    def __init__(self, loader):
        self._loader = loader

    def create_module(self, spec):
        return self._loader.create_module(spec)

    def exec_module(self, module):
        self._loader.exec_module(module)
        try:
            import ov_nav_patch

            ov_nav_patch.apply(module)
        except Exception as exc:  # noqa: BLE001 — a patch failure must never break the import
            print(f"ov-nav-patch: install failed: {exc!r}", file=sys.stderr)


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname != TARGET:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is not None and spec.loader is not None:
            spec.loader = _PatchingLoader(spec.loader)
        return spec


if not any(isinstance(f, _Finder) for f in sys.meta_path):
    sys.meta_path.insert(0, _Finder())
