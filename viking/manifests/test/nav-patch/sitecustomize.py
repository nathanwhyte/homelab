"""Post-import hook that installs the OpenViking v0.4.20 runtime patches.

Mounted from a ConfigMap onto PYTHONPATH. Python's ``site`` imports this file at
interpreter start in every process, including the uvicorn workers. Each patch is
installed only in a process that imports its target module, so the ``ov`` CLI and
the entrypoint's helper interpreters are unaffected.

Patches (each guarded on the installed version + a source hash, each with its own
off switch; a patch failure never breaks the import):

  * ov_nav_patch      — IMPR-1185: deterministic Quick Navigation in directory
                        overviews (``SemanticProcessor._generate_overview``).
  * ov_extract_patch  — BUG-1176: a degraded memory extraction (empty/unparseable
                        LLM output after the loop's own retry) becomes a retryable
                        Phase 2 failure instead of an empty ``memory_diff.json``
                        recorded as success (``ExtractLoop.run`` + the session's
                        Phase 2 retry classifier and budget).

Rollback of everything: remove PYTHONPATH from the Deployment
(``kubectl set env … PYTHONPATH-``). Rollback of one patch: its env switch.
"""

import importlib.abc
import importlib.machinery
import sys

# target module -> (patch module, apply function name)
TARGETS = {
    "openviking.storage.queuefs.semantic_processor": ("ov_nav_patch", "apply"),
    "openviking.session.memory.extract_loop": (
        "ov_extract_patch",
        "apply_extract_loop",
    ),
    "openviking.session.session": ("ov_extract_patch", "apply_session"),
}


class _PatchingLoader(importlib.abc.Loader):
    def __init__(self, loader, patch_module, apply_name):
        self._loader = loader
        self._patch_module = patch_module
        self._apply_name = apply_name

    def create_module(self, spec):
        return self._loader.create_module(spec)

    def exec_module(self, module):
        self._loader.exec_module(module)
        try:
            patch = __import__(self._patch_module)
            getattr(patch, self._apply_name)(module)
        except Exception as exc:  # noqa: BLE001 — a patch failure must never break the import
            print(
                f"{self._patch_module}: install failed for {module.__name__}: {exc!r}",
                file=sys.stderr,
            )


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        entry = TARGETS.get(fullname)
        if entry is None:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is not None and spec.loader is not None:
            spec.loader = _PatchingLoader(spec.loader, *entry)
        return spec


if not any(isinstance(f, _Finder) for f in sys.meta_path):
    sys.meta_path.insert(0, _Finder())
