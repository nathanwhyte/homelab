"""Post-import hook that installs the OpenViking v0.4.20 runtime patches.

Mounted from a ConfigMap onto PYTHONPATH. Python's ``site`` imports this file at
interpreter start in every process, including the uvicorn workers. Each patch is
installed only in a process that imports its target module, so the ``ov`` CLI and
the entrypoint's helper interpreters are unaffected.

Patches (each guarded on the installed version + a source hash, each with its own
off switch; a patch failure never breaks the import):

  * ov_s3_cache_patch — BUG-1174: disable process-local S3 metadata caches so a
                        worker sees directories created by another worker.
  * ov_nav_patch      — IMPR-1185: deterministic Quick Navigation in directory
                        overviews (``SemanticProcessor._generate_overview``).
  * ov_extract_patch  — BUG-1176: a degraded memory extraction (empty/unparseable
                        LLM output after the loop's own retry) becomes a retryable
                        Phase 2 failure instead of an empty ``memory_diff.json``
                        recorded as success (``ExtractLoop.run`` + the session's
                        Phase 2 retry classifier and budget).
  * ov_chatlog_patch  — BUG-1177: ChatLog speaker labels follow the turn's role
                        (assistant turns read ``assistant``) and tool-only turns no
                        longer render as empty lines (``MessageRange._speaker_for`` +
                        ``_format_contiguous_group``).
  * ov_memory_guard_patch — BUG-1180: an ``add_only`` memory write (events,
                        trajectories) never replaces an existing memory; an exact
                        duplicate is dropped and a collision goes to a free sibling
                        path (``MemoryUpdater.apply_operations``); its echo
                        guard cuts pasted OpenViking recall output from user
                        turns before extraction (``ExtractContext.__init__``,
                        ``OV_ECHO_GUARD=0``).

Rollback of one patch: its env switch (``OV_S3_CACHE_PATCH=0``, ``OV_EXTRACT_PATCH=0``, ``OV_CHATLOG_PATCH=0``, ``OV_MEMORY_GUARD=0``; ``OV_NAV_PATCH=0``
only after the model-built overview template is restored, see the Deployment).
Rollback of everything: remove PYTHONPATH from the Deployment
(``kubectl set env … PYTHONPATH-``), again only after that template restore.
"""

import importlib.abc
import importlib.machinery
import sys

# target module -> (patch module, apply function name)
TARGETS = {
    "openviking.utils.agfs_utils": ("ov_s3_cache_patch", "apply"),
    "openviking.storage.queuefs.semantic_processor": ("ov_nav_patch", "apply"),
    "openviking.session.memory.extract_loop": (
        "ov_extract_patch",
        "apply_extract_loop",
    ),
    "openviking.session.session": ("ov_extract_patch", "apply_session"),
    "openviking.session.memory.memory_updater": [
        ("ov_chatlog_patch", "apply"),
        ("ov_memory_guard_patch", "apply"),
        ("ov_memory_guard_patch", "apply_echo_guard"),
    ],
}


def _entries(entry):
    """A target maps to one ``(patch, apply)`` pair or a list of them, applied in order."""
    return [entry] if isinstance(entry, tuple) else list(entry)


class _PatchingLoader(importlib.abc.Loader):
    def __init__(self, loader, entries):
        self._loader = loader
        self._entries = entries

    def create_module(self, spec):
        return self._loader.create_module(spec)

    def exec_module(self, module):
        self._loader.exec_module(module)
        for patch_module, apply_name in self._entries:
            try:
                patch = __import__(patch_module)
                getattr(patch, apply_name)(module)
            except Exception as exc:  # noqa: BLE001 — a patch failure must never break the import
                print(
                    f"{patch_module}: install failed for {module.__name__}: {exc!r}",
                    file=sys.stderr,
                )


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        entry = TARGETS.get(fullname)
        if entry is None:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is not None and spec.loader is not None:
            spec.loader = _PatchingLoader(spec.loader, _entries(entry))
        return spec


if not any(isinstance(f, _Finder) for f in sys.meta_path):
    sys.meta_path.insert(0, _Finder())
