#!/usr/bin/env python3
"""Preparation/recovery contract tests; fake Ollama, no model or systemd calls."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "_scripts" / "lib"))
from git_env import scrub_environ  # noqa: E402

scrub_environ()

import copy  # noqa: E402
import errno  # noqa: E402
import fcntl  # noqa: E402
import hashlib  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import signal  # noqa: E402
import stat  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
import textwrap  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import unittest  # noqa: E402
import urllib.error  # noqa: E402
from contextlib import contextmanager  # noqa: E402
from unittest import mock  # noqa: E402

HOST = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOST))
import ollama_prepare as module  # noqa: E402
from ollama_prepare import Posture, Preparation  # noqa: E402
from ollama_recovery import (  # noqa: E402
    Http,
    PreparationError,
    TransportUnavailable,
    recover,
)


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class FakeOllama:
    """Consumer adapter returns canonical manifests, not a Modelfile parser."""

    url = "http://fixture.invalid"

    def __init__(self):
        self.models = {}
        self.running = []
        self.events = []
        self.version = "0.40.11"
        self.drop_copy = False
        self.drop_delete = False
        self.copy_error = False
        self.load_error = False
        self.evict = False
        self.incomplete_create = False
        self.on_show = None

    def tags(self):
        return {name: data["digest"] for name, data in self.models.items()}

    def show(self, tag):
        if self.on_show:
            self.on_show(tag)
        return copy.deepcopy(self.models[tag]["show"])

    def create(self, tag, recipe):
        self.events.append(("create", tag, recipe.name))
        # Fixture recipes contain fake consumer output. The production code
        # must call its consumer adapter; it must never interpret these bytes.
        shown = json.loads(recipe.read_text())
        if self.incomplete_create:
            return
        sha = hashlib.sha256(module.encoded(shown).encode()).hexdigest()
        self.models[tag] = {"digest": sha, "show": shown}

    def call(self, path, body=None, method=None, timeout=None):
        if path == "/api/version":
            return {"version": self.version}
        self.events.append((path, copy.deepcopy(body)))
        if path == "/api/copy":
            if self.copy_error:
                raise PreparationError("copy transport failed")
            if not self.drop_copy:
                self.models[body["destination"]] = copy.deepcopy(
                    self.models[body["source"]]
                )
        elif path == "/api/delete":
            if not self.drop_delete:
                self.models.pop(body["model"], None)
        else:
            raise AssertionError(path)
        return {}

    def loaded(self):
        return copy.deepcopy(self.running)

    def load(self, tag):
        self.events.append(("load", tag))
        if self.load_error:
            raise PreparationError("load failed")
        if self.evict:
            self.running = []
        self.running = [item for item in self.running if item["name"] != tag]
        self.running.append(
            {
                "name": tag,
                "digest": self.models[tag]["digest"],
                "context_length": module.integer_parameter(self.show(tag), "num_ctx"),
                "size_vram": 100,
            }
        )


class FakeRuntime:
    def __init__(self, error=False):
        self.error = error
        self.stops = 0
        self.current = True
        self.generation_checks = 0

    def _assert_current(self):
        self.generation_checks += 1
        if not self.current:
            raise PreparationError("captured daemon generation changed")

    def verify(self, expected, models):
        if self.error:
            raise PreparationError("runtime mismatch")
        return {
            "boot_id": "boot",
            "invocation_id": "invocation",
            "pid": 42,
            "start_ticks": 7,
            "models": models,
        }

    def stop_failed(self):
        self._assert_current()
        self.stops += 1
        return {"stopped": True}


class FixtureCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.recipes = self.root / "modelfiles"
        self.recipes.mkdir()
        self.config = self.root / "model-posture.json"
        self.data = json.loads((HOST / "model-posture.json").read_text())
        self.config.write_text(json.dumps(self.data))
        for tag, spec in self.data["models"].items():
            blobs = spec.get("blobs", [hashlib.sha256(tag.encode()).hexdigest()])
            shown = {
                "modelfile": "\n".join(
                    "FROM /store/blobs/sha256-" + sha for sha in blobs
                )
                + '\nTEMPLATE """safe"""',
                "parameters": "num_ctx 65536\nnum_batch 2048\ntemperature 0"
                if tag == "gemma4:vlm"
                else "num_ctx 8192\ntemperature 0",
                "renderer": "gemma4-small" if tag == "gemma4:vlm" else "",
                "parser": "gemma4" if tag == "gemma4:vlm" else "",
                "template": "fixture template",
                "model_info": {"general.name": tag},
            }
            (self.recipes / spec["recipe"]).write_text(json.dumps(shown))
        self.api = FakeOllama()
        self.make_preparation()

    def make_preparation(self, resident=None):
        self.posture = Posture(self.config, self.recipes, resident)
        self.prep = Preparation(
            self.posture, self.api, self.root / "state", self.api.create
        )
        self.prep.state.mkdir(exist_ok=True)

    def mutate_recipe(self, tag, change):
        path = self.posture.recipe(tag)
        shown = json.loads(path.read_text())
        change(shown)
        path.write_text(json.dumps(shown))
        self.make_preparation(self.posture.resident)

    def assert_no_candidates(self):
        self.assertFalse(
            any(tag.startswith("homelab-preparing/") for tag in self.api.tags())
        )


class PreparationTests(FixtureCase):
    def test_waiting_caller_snapshots_changed_recipe_and_config_after_lock(self):
        stale_revision = self.posture.revision
        waiting = threading.Event()
        results = []
        errors = []
        real_flock = fcntl.flock
        lock = self.prep.state / "preparation.lock"
        with lock.open("a+") as owner:
            real_flock(owner, fcntl.LOCK_EX)

            def observe_flock(fd, operation):
                waiting.set()
                return real_flock(fd, operation)

            def work():
                try:
                    with self.prep.locked():
                        results.append(self.prep.warm(FakeRuntime()))
                except BaseException as exc:
                    errors.append(exc)

            with mock.patch.object(module.fcntl, "flock", observe_flock):
                worker = threading.Thread(target=work)
                worker.start()
                try:
                    self.assertTrue(waiting.wait(5), "worker did not reach lock")
                    recipe = self.posture.recipe("gemma4:vlm")
                    shown = json.loads(recipe.read_text())
                    shown["template"] = "replacement installed while caller waited"
                    recipe.write_text(json.dumps(shown))
                    self.data["parallel"] += 1
                    self.config.write_text(json.dumps(self.data))
                    expected = Posture(self.config, self.recipes)
                finally:
                    real_flock(owner, fcntl.LOCK_UN)
                    worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertNotEqual(stale_revision, expected.revision)
        self.assertEqual(results[0]["revision"], expected.revision)
        self.assertEqual(self.api.show("gemma4:vlm")["template"], shown["template"])
        self.assertEqual(self.prep.posture.data["parallel"], self.data["parallel"])

    def test_compiler_reads_captured_bytes_beside_recipe_with_modelfile_suffix(self):
        tag = "gemma4:vlm"
        original = self.posture.recipe_bytes[tag]
        seen = []

        def create(name, snapshot):
            seen.append(snapshot)
            self.assertEqual(snapshot.parent, self.recipes.resolve())
            self.assertEqual(snapshot.suffix, ".Modelfile")
            self.assertEqual(snapshot.read_bytes(), original)
            self.api.create(name, snapshot)

        self.prep.creator = create
        with self.prep.locked():
            revision = self.prep.posture.revision
            self.posture.recipe(tag).write_text("not a valid fixture recipe anymore")
            result = self.prep.prepare([tag])
        self.assertEqual(result[tag]["context"], 65536)
        self.assertEqual(revision, self.prep.posture.revision)
        self.assertTrue(seen)
        self.assertTrue(all(not path.exists() for path in seen))

    def test_inherited_lock_is_validated_and_remains_owned_after_context(self):
        lock = self.prep.state / "preparation.lock"
        with lock.open("a+") as owner, lock.open("a+") as competitor:
            lock.chmod(0o600)
            self.prep.state.chmod(0o700)
            fcntl.flock(owner, fcntl.LOCK_EX)
            self.prep.inherited_lock_fd = owner.fileno()
            with self.prep.locked():
                self.assertNotEqual(self.prep._lock_fd, owner.fileno())
                self.assertEqual(lock.stat().st_mode & 0o777, 0o644)
                self.assertEqual(self.prep.state.stat().st_mode & 0o777, 0o755)
            with self.assertRaises(BlockingIOError):
                fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(owner, fcntl.LOCK_UN)
            fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with (self.root / "unrelated.lock").open("a+") as unrelated:
            self.prep.inherited_lock_fd = unrelated.fileno()
            with self.assertRaisesRegex(PreparationError, "does not match"):
                with self.prep.locked():
                    self.fail("wrong inherited inode accepted")

    def test_interrupted_candidate_is_replayed_from_exact_durable_intent(self):
        unrelated = "homelab-preparing/operator:0123456789abcdef0123456789abcdef"
        self.api.models[unrelated] = {"digest": "a" * 64, "show": {}}

        def interrupted_create(name, snapshot):
            intent = next((self.prep.state / "candidate-intents").glob("*.json"))
            self.assertEqual(json.loads(intent.read_text())["tag"], name)
            self.api.create(name, snapshot)
            raise InterruptedError("cancelled fixture create")

        self.prep.creator = interrupted_create
        with self.prep.locked(), self.assertRaises(InterruptedError):
            self.prep.prepare()
        intents = list((self.prep.state / "candidate-intents").glob("*.json"))
        self.assertEqual(len(intents), 1)
        pending = json.loads(intents[0].read_text())
        self.assertIn(pending["tag"], self.api.tags())
        self.prep.creator = self.api.create
        with self.prep.locked():
            self.prep.prepare()
        self.assertNotIn(pending["tag"], self.api.tags())
        self.assertIn(unrelated, self.api.tags())
        self.assertFalse(Path(pending["snapshot"]).exists())
        self.assertEqual(list((self.prep.state / "candidate-intents").iterdir()), [])

    def test_failed_replay_keeps_intent_until_confirmed_delete(self):
        self.api.drop_delete = True
        with self.prep.locked(), self.assertRaisesRegex(PreparationError, "cleanup"):
            self.prep.prepare()
        intents = list((self.prep.state / "candidate-intents").glob("*.json"))
        self.assertEqual(len(intents), 1)
        with self.prep.locked(), self.assertRaisesRegex(PreparationError, "cleanup"):
            self.prep.prepare()
        self.assertTrue(intents[0].exists())
        self.api.drop_delete = False
        with self.prep.locked():
            self.prep.prepare()
        self.assertFalse(intents[0].exists())
        self.assert_no_candidates()

    def test_invalid_intent_cannot_authorize_other_model_deletion(self):
        token = "a" * 32
        self.api.models["operator:keep"] = {"digest": "b" * 64, "show": {}}
        intent = self.prep.intent_directory() / (token + ".json")
        intent.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "tag": "operator:keep",
                    "snapshot": str(
                        self.recipes / (".ollama-preparing-" + token + ".Modelfile")
                    ),
                }
            )
        )
        with (
            self.prep.locked(),
            self.assertRaisesRegex(PreparationError, "unrelated cleanup"),
        ):
            self.prep.prepare()
        self.assertIn("operator:keep", self.api.tags())
        self.assertTrue(intent.exists())

    def test_terminated_producer_holds_lock_until_child_exits_then_replays(self):
        fixture_bin = self.root / "bin"
        fixture_bin.mkdir()
        ready = self.root / "child-ready.json"
        release = self.root / "release-child"
        finished = self.root / "child-finished"
        child_source = self.root / "fake-ollama.py"
        child_source.write_text(
            "#!"
            + sys.executable
            + "\n"
            + textwrap.dedent("""
            import json, os, pathlib, sys, time
            root = pathlib.Path(os.environ['PRODUCER_FIXTURE'])
            (root / 'child-ready.json').write_text(json.dumps({'tag': sys.argv[2], 'snapshot': sys.argv[4]}))
            deadline = time.monotonic() + 15
            while not (root / 'release-child').exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            (root / 'child-finished').touch()
        """)
        )
        child_source.chmod(0o755)
        (fixture_bin / "ollama").symlink_to(child_source)
        worker_source = self.root / "producer.py"
        worker_source.write_text(
            textwrap.dedent("""
            import pathlib, sys
            sys.path.insert(0, sys.argv[1])
            from ollama_prepare import Preparation
            class Api:
                url = 'http://fixture.invalid'
                def call(self, path, **kwargs): return {'version': '0.40.11'}
                def tags(self): return {}
            root = pathlib.Path(sys.argv[2])
            prep = Preparation(None, Api(), root / 'state', source=(root / 'model-posture.json', root / 'modelfiles', None))
            with prep.locked():
                prep.prepare()
        """)
        )
        worker = subprocess.Popen(
            [sys.executable, str(worker_source), str(HOST), str(self.root)],
            env=dict(
                os.environ,
                PATH=str(fixture_bin) + os.pathsep + os.environ["PATH"],
                PRODUCER_FIXTURE=str(self.root),
            ),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready.exists(), "controlled create child did not start")
            worker.terminate()
            worker.wait(timeout=5)
            pending = json.loads(ready.read_text())
            with (self.prep.state / "preparation.lock").open("r") as probe:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                release.touch()
                deadline = time.monotonic() + 5
                while True:
                    try:
                        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            self.fail("create child did not release inherited lock")
                        time.sleep(0.01)
            self.assertTrue(finished.exists())
            self.api.create(pending["tag"], Path(pending["snapshot"]))
            with self.prep.locked():
                self.prep.prepare()
            self.assertNotIn(pending["tag"], self.api.tags())
            self.assertFalse(Path(pending["snapshot"]).exists())
            self.assertEqual(
                list((self.prep.state / "candidate-intents").iterdir()), []
            )
        finally:
            release.touch()
            if worker.poll() is None:
                worker.terminate()
            worker.wait(timeout=5)

    def test_missing_models_are_created_read_back_and_not_loaded(self):
        result = self.prep.prepare()
        self.assertEqual(set(result), set(self.posture.required))
        self.assertFalse(self.api.running)
        self.assert_no_candidates()
        self.assertFalse((self.prep.state / "verified.json").exists())

    def test_noncontext_recipe_changes_are_reconciled(self):
        for field, value in [
            ("parameters", "num_ctx 65536\nnum_batch 1024\ntemperature 0.5"),
            ("template", "new template"),
            ("parser", "new-parser"),
            ("renderer", "new-renderer"),
        ]:
            with self.subTest(field=field):
                self.prep.prepare()
                old = self.api.tags()["gemma4:vlm"]
                self.mutate_recipe(
                    "gemma4:vlm", lambda shown: shown.update({field: value})
                )
                result = self.prep.prepare()
                self.assertNotEqual(old, result["gemma4:vlm"]["digest"])
                self.assertEqual(self.api.show("gemma4:vlm")[field], value)
                self.assert_no_candidates()

    def test_unchanged_replay_does_not_rewrite_serving_tags(self):
        self.prep.prepare()
        self.api.events.clear()
        self.prep.prepare()
        self.assertFalse(any(event[0] == "/api/copy" for event in self.api.events))
        self.assert_no_candidates()

    def test_weights_and_projector_each_block_before_adoption(self):
        for index in (0, 1):
            with self.subTest(index=index):
                wanted = self.data["models"]["gemma4:vlm"]["blobs"][index]
                self.mutate_recipe(
                    "gemma4:vlm",
                    lambda shown: shown.update(
                        modelfile=shown["modelfile"].replace(wanted, "0" * 64)
                    ),
                )
                with self.assertRaisesRegex(PreparationError, "pins"):
                    self.prep.prepare()
                self.assertNotIn("gemma4:vlm", self.api.tags())
                self.assert_no_candidates()
                # Restore this fixture for the next independently tested pin.
                self.mutate_recipe(
                    "gemma4:vlm",
                    lambda shown: shown.update(
                        modelfile=shown["modelfile"].replace("0" * 64, wanted)
                    ),
                )

    def test_create_and_copy_acknowledgements_are_not_proof(self):
        self.api.incomplete_create = True
        with self.assertRaisesRegex(PreparationError, "readable candidate"):
            self.prep.prepare()
        self.api.incomplete_create = False
        self.api.drop_copy = True
        with self.assertRaisesRegex(PreparationError, "did not adopt"):
            self.prep.prepare()
        self.assert_no_candidates()

    def test_copy_failure_cleans_only_owned_candidate(self):
        self.api.copy_error = True
        self.api.models["operator:keep"] = {"digest": "a" * 64, "show": {}}
        with self.assertRaisesRegex(PreparationError, "transport"):
            self.prep.prepare()
        self.assertEqual(set(self.api.tags()), {"operator:keep"})

    def test_cleanup_failure_is_not_hidden(self):
        self.api.drop_delete = True
        with self.assertRaisesRegex(PreparationError, "cleanup"):
            self.prep.prepare()

    def test_wrong_daemon_version_does_not_create_or_load(self):
        self.api.version = "0.40.10"
        with self.assertRaisesRegex(PreparationError, "version"):
            self.prep.prepare()
        self.assertEqual(self.api.events, [])

    def test_warm_atomically_publishes_receipt_after_runtime_proof(self):
        runtime = FakeRuntime()
        receipt = self.prep.warm(runtime)
        self.assertEqual(runtime.stops, 0)
        self.assertEqual(receipt["revision"], self.posture.revision)
        self.assertEqual(receipt["schema"], 2)
        self.assertEqual(receipt["residents"], self.posture.residents)
        self.assertEqual(receipt["version"], self.data["version"])
        self.assertFalse(
            any(tag.startswith("homelab-prepared/") for tag in self.api.tags())
        )
        self.assertEqual(
            json.loads((self.prep.state / "verified.json").read_text()), receipt
        )

    def test_startup_transport_wait_invalidates_receipt_before_any_probe(self):
        clock = FakeClock()
        runtime = FakeRuntime()
        receipt = self.prep.state / "verified.json"
        receipt.write_text('{"old": true}')
        call = self.api.call
        probes = []

        def probe(path, body=None, method=None, timeout=None):
            if path == "/api/version" and timeout is not None:
                self.assertFalse(receipt.exists())
                probes.append(timeout)
                if len(probes) < 3:
                    raise TransportUnavailable("connection refused")
            return call(path, body, method)

        with (
            mock.patch.object(self.api, "call", side_effect=probe),
            mock.patch.object(module.time, "monotonic", clock.monotonic),
            mock.patch.object(module.time, "sleep", clock.sleep),
        ):
            result = self.prep.warm(runtime)
        self.assertEqual(probes, [1.0, 1.0, 1.0])
        self.assertEqual(clock.sleeps, [1.0, 1.0])
        self.assertGreaterEqual(runtime.generation_checks, 6)
        self.assertEqual(runtime.stops, 0)
        self.assertEqual(result["revision"], self.posture.revision)

    def test_failed_startup_wait_clears_receipt_and_reports_unreachable_witnesses(self):
        self.prep.warm(FakeRuntime())
        before = self.api.tags()
        self.api.events.clear()
        clock = FakeClock()
        runtime = FakeRuntime()
        with (
            mock.patch.object(
                self.api, "call", side_effect=TransportUnavailable("refused")
            ) as calls,
            mock.patch.object(
                self.api, "tags", side_effect=TransportUnavailable("refused")
            ),
            mock.patch.object(module.time, "monotonic", clock.monotonic),
            mock.patch.object(module.time, "sleep", clock.sleep),
            mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            with self.assertRaisesRegex(
                PreparationError,
                "captured daemon stopped.*startup readiness timed out after 30s",
            ):
                self.prep.warm(runtime)
        self.assertEqual(clock.now, 30.0)
        self.assertEqual(calls.call_count, 30)
        self.assertEqual(runtime.stops, 1)
        self.assertIn("verification invalidation failed: refused", stderr.getvalue())
        self.assertFalse((self.prep.state / "verified.json").exists())
        # HTTP was unreachable: no claim that persistent aliases were deleted.
        self.assertEqual(self.api.tags(), before)
        self.assertEqual(self.api.events, [])

    def test_next_successful_startup_removes_old_witnesses_before_prepare(self):
        self.prep.warm(FakeRuntime())
        self.api.models["homelab-prepared/retired-model-old:" + "b" * 64] = {
            "digest": "a" * 64,
            "show": {},
        }
        old_witnesses = {
            tag for tag in self.api.tags() if tag.startswith("homelab-prepared/")
        }
        self.api.events.clear()
        self.prep.warm(FakeRuntime())
        first_create = next(
            i for i, event in enumerate(self.api.events) if event[0] == "create"
        )
        removed = {
            event[1]["model"]
            for event in self.api.events[:first_create]
            if event[0] == "/api/delete"
        }
        self.assertEqual(removed, old_witnesses)

    def test_cancelled_publication_cannot_leave_recovery_acceptance(self):
        write = module.atomic_write
        for after_commit in (False, True):
            with self.subTest(after_commit=after_commit):
                runtime = FakeRuntime()

                def cancel(path, content):
                    if Path(path).name != "verified.json":
                        return write(path, content)
                    if after_commit:
                        write(path, content)
                    raise KeyboardInterrupt("cancelled adoption")

                with mock.patch.object(module, "atomic_write", side_effect=cancel):
                    with self.assertRaisesRegex(
                        PreparationError, "captured daemon stopped"
                    ):
                        self.prep.warm(runtime)
                self.assertEqual(runtime.stops, 1)
                self.assertFalse((self.prep.state / "verified.json").exists())
                self.assertFalse(
                    any(tag.startswith("homelab-prepared/") for tag in self.api.tags())
                )

    def test_invalidation_retires_removed_catalog_witnesses_only(self):
        retired = "homelab-prepared/removed-resident-old:" + "a" * 64
        unrelated = "homelab-prepared/operator:notes"
        self.api.models[retired] = {"digest": "a" * 64, "show": {}}
        self.api.models[unrelated] = {"digest": "b" * 64, "show": {}}
        self.api.models["operator:keep"] = {"digest": "c" * 64, "show": {}}
        self.prep.invalidate()
        self.assertEqual(set(self.api.tags()), {unrelated, "operator:keep"})

    def test_startup_wait_does_not_retry_version_or_protocol_mismatch(self):
        for failure in ({"version": "0.40.10"}, PreparationError("invalid JSON")):
            with self.subTest(failure=failure):
                clock = FakeClock()
                runtime = FakeRuntime()
                with (
                    mock.patch.object(self.api, "call", side_effect=[failure]) as call,
                    mock.patch.object(module.time, "monotonic", clock.monotonic),
                    mock.patch.object(module.time, "sleep", clock.sleep),
                ):
                    with self.assertRaises(PreparationError):
                        self.prep.warm(runtime)
                self.assertEqual(clock.sleeps, [])
                self.assertEqual(call.call_count, 1)
                self.assertEqual(runtime.stops, 1)
                self.assertEqual(self.api.events, [])

    def test_generation_change_during_wait_never_adopts_replacement(self):
        runtime = FakeRuntime()
        clock = FakeClock()

        def replaced(seconds):
            clock.sleep(seconds)
            runtime.current = False

        with (
            mock.patch.object(
                self.api, "call", side_effect=TransportUnavailable("refused")
            ) as call,
            mock.patch.object(module.time, "monotonic", clock.monotonic),
            mock.patch.object(module.time, "sleep", replaced),
        ):
            with self.assertRaisesRegex(
                PreparationError, "STOP FAILED: captured daemon generation changed"
            ):
                self.prep.warm(runtime)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(runtime.stops, 0)
        self.assertFalse((self.prep.state / "verified.json").exists())
        self.assertEqual(self.api.events, [])

    def test_probe_timeout_and_sleep_are_limited_by_remaining_deadline(self):
        runtime = FakeRuntime()
        clock = FakeClock()
        timeouts = []

        def slow_probe(path, timeout):
            timeouts.append(timeout)
            clock.now += timeout
            raise TransportUnavailable("timed out")

        with (
            mock.patch.object(self.api, "call", side_effect=slow_probe),
            mock.patch.object(module.time, "monotonic", clock.monotonic),
            mock.patch.object(module.time, "sleep", clock.sleep),
        ):
            with self.assertRaisesRegex(PreparationError, "timed out after 2.5s"):
                self.prep.wait_ready(runtime, timeout=2.5)
        self.assertEqual(timeouts, [1.0, 0.5])
        self.assertEqual(clock.sleeps, [1.0])
        self.assertEqual(clock.now, 2.5)

    def test_prepare_remains_immediate_on_unavailable_transport(self):
        with (
            mock.patch.object(
                self.api, "call", side_effect=TransportUnavailable("refused")
            ) as call,
            mock.patch.object(module.time, "sleep") as sleep,
        ):
            with self.assertRaises(TransportUnavailable):
                self.prep.prepare()
        self.assertEqual(call.call_count, 1)
        sleep.assert_not_called()

    def test_runtime_or_load_failure_stops_and_never_certifies(self):
        for load_error in (False, True):
            with self.subTest(load_error=load_error):
                runtime = FakeRuntime(error=True)
                self.api.load_error = load_error
                with self.assertRaisesRegex(PreparationError, "adoption failed"):
                    self.prep.warm(runtime)
                self.assertEqual(runtime.stops, 1)
                self.assertFalse((self.prep.state / "verified.json").exists())
                self.assertFalse(
                    any(tag.startswith("homelab-prepared/") for tag in self.api.tags())
                )

    def test_all_four_choices_and_the_corpresident_set(self):
        for choice, profile in self.data["profiles"].items():
            with self.subTest(choice=choice):
                self.api = FakeOllama()
                self.make_preparation(choice)
                self.prep.warm(FakeRuntime())
                self.assertEqual(
                    [row["name"] for row in self.api.running], profile["residents"]
                )
                self.assertEqual(
                    self.posture.expected_runtime()["max_loaded"],
                    len(profile["residents"]),
                )

    def test_copresident_eviction_fails_adoption(self):
        self.make_preparation("qwen2.5-coder:fim")
        self.api.evict = True
        runtime = FakeRuntime()
        with self.assertRaisesRegex(PreparationError, "loaded models"):
            self.prep.warm(runtime)
        self.assertEqual(runtime.stops, 1)

    def test_files_only_checks_byte_identity_and_recovery_revision(self):
        installed_root = self.root / "installed"
        installed_recipes = installed_root / "modelfiles"
        installed_recipes.mkdir(parents=True)
        (installed_root / "model-posture.json").write_text(self.config.read_text())
        for source in self.recipes.iterdir():
            # JSON whitespace represents a semantically identical recipe in
            # this fake consumer, just as comments can for the real consumer.
            (installed_recipes / source.name).write_text(
                json.dumps(json.loads(source.read_text()), indent=4)
            )
        old = Posture(installed_root / "model-posture.json", installed_recipes)
        with self.assertRaisesRegex(PreparationError, "recipe revision"):
            self.prep.files_check(old)
        self.assert_no_candidates()
        for source in self.recipes.iterdir():
            (installed_recipes / source.name).write_text(source.read_text())
        old = Posture(installed_root / "model-posture.json", installed_recipes)
        self.prep.files_check(old)
        self.mutate_recipe(
            "gemma4:vlm", lambda shown: shown.update(template="different")
        )
        with self.assertRaisesRegex(PreparationError, "recipe revision"):
            self.prep.files_check(old)
        self.assert_no_candidates()

    def test_files_only_after_default_adoption_does_not_pull_unused_rollback_bases(
        self,
    ):
        create = self.api.create

        def selected_only(tag, recipe):
            if (
                "deepseek"
                in json.loads(recipe.read_text())["model_info"]["general.name"]
            ):
                raise PreparationError("unused rollback base unavailable")
            return create(tag, recipe)

        self.prep.creator = selected_only
        self.prep.warm(FakeRuntime())
        self.api.events.clear()
        installed = Posture(self.config, self.recipes)
        self.prep.files_check(installed)
        self.assertEqual(self.api.events, [])
        self.assertEqual({row["name"] for row in self.api.running}, {"gemma4:vlm"})

    def test_files_only_refuses_configuration_or_any_recipe_bytes(self):
        installed = Posture(self.config, self.recipes)
        for tag in self.data["models"]:
            with self.subTest(tag=tag):
                path = self.posture.recipe(tag)
                original = path.read_text()
                # A byte-only difference leaves the fixture consumer output unchanged.
                path.write_text(original + "\n")
                self.make_preparation()
                with self.assertRaisesRegex(PreparationError, "recipe revision"):
                    self.prep.files_check(installed)
                path.write_text(original)
                self.make_preparation()
        self.data["parallel"] = self.data["parallel"] + 1
        self.config.write_text(json.dumps(self.data))
        self.make_preparation()
        with self.assertRaisesRegex(PreparationError, "posture/settings"):
            self.prep.files_check(installed)
        self.assertEqual(self.api.events, [])

    def test_cli_prepare_uses_same_contract_and_lock(self):
        argv = [
            "ollama_prepare.py",
            "--config",
            str(self.config),
            "--recipes",
            str(self.recipes),
            "--state",
            str(self.prep.state),
            "prepare",
        ]
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(module, "Http", return_value=self.api),
            mock.patch.object(Preparation, "create", side_effect=self.api.create),
            mock.patch("sys.stdout", new_callable=io.StringIO) as output,
        ):
            module.main()
        self.assertEqual(set(json.loads(output.getvalue())), set(self.posture.required))
        self.assertTrue((self.prep.state / "preparation.lock").exists())

    def test_receipt_requires_current_generation_and_current_manifest(self):
        runtime = FakeRuntime()
        self.prep.warm(runtime)
        self.assertEqual(self.prep.verify(runtime)["status"], "verified")
        with mock.patch.object(
            runtime,
            "verify",
            return_value={
                "boot_id": "other",
                "invocation_id": "invocation",
                "pid": 42,
                "start_ticks": 7,
            },
        ):
            with self.assertRaisesRegex(
                PreparationError, "different daemon generation"
            ):
                self.prep.verify(runtime)
        self.api.models["gemma4:vlm"]["digest"] = "0" * 64
        with self.assertRaisesRegex(PreparationError, "identity"):
            self.prep.verify(runtime, stop_on_failure=True)
        self.assertEqual(runtime.stops, 1)

    def test_stop_failure_is_explicit_and_never_claims_stopped(self):
        runtime = FakeRuntime(error=True)
        with mock.patch.object(
            runtime, "stop_failed", side_effect=RuntimeError("permission denied")
        ):
            with self.assertRaisesRegex(
                PreparationError, "STOP FAILED: permission denied"
            ):
                self.prep.warm(runtime)

    def test_render_derives_host_and_recovery_from_same_selected_posture(self):
        for resident, profile in self.data["profiles"].items():
            with self.subTest(resident=resident):
                self.make_preparation(resident)
                result = module.render(self.posture, self.root / "rendered", False)
                self.assertEqual(result["revision"], self.posture.revision)
                generated = self.root / "rendered/llama"
                dropin = (generated / "host/ollama.service.d/homelab.conf").read_text()
                self.assertIn(
                    f"OLLAMA_MAX_LOADED_MODELS={len(profile['residents'])}", dropin
                )
                recovery = json.loads(
                    (generated / "ollama-jobs.yaml")
                    .read_text()
                    .split("\n", 1)[1]
                    .split("\n---\n", 1)[0]
                )
                self.assertIs(recovery["spec"]["suspend"], False)
                env = recovery["spec"]["jobTemplate"]["spec"]["template"]["spec"][
                    "containers"
                ][0]["env"]
                config = json.loads(
                    next(
                        row["value"] for row in env if row["name"] == "OLLAMA_RECOVERY"
                    )
                )
                self.assertEqual(set(config["residents"]), set(profile["residents"]))
                self.assertEqual(
                    module.render(self.posture, self.root / "rendered", True)[
                        "changed"
                    ],
                    [],
                )
        (generated / "ollama-jobs.yaml").write_text("stale")
        with self.assertRaisesRegex(PreparationError, "stale"):
            module.render(self.posture, self.root / "rendered", True)


class ProfileSelectionTests(FixtureCase):
    REQUIRED = {
        "gemma4:vlm": {"gemma4:vlm", "qwen2.5-coder:fim"},
        "qwen2.5-coder:fim": {
            "qwen2.5-coder:fim",
            "gemma4:vlm",
            "deepseek-coder-v2:instruct",
        },
        "deepseek-coder-v2:fim": {
            "deepseek-coder-v2:fim",
            "deepseek-coder-v2:instruct",
            "qwen2.5-coder:fim",
            "gemma4:vlm",
        },
        "deepseek-coder-v2:instruct": {
            "deepseek-coder-v2:instruct",
            "deepseek-coder-v2:fim",
            "qwen2.5-coder:fim",
            "gemma4:vlm",
        },
    }
    PAIR = {"qwen2.5-coder:fim", "gemma4:vlm"}

    def cli(self, *args, ambient="not-a-profile"):
        argv = [
            "ollama_prepare.py",
            "--config",
            str(self.config),
            "--recipes",
            str(self.recipes),
            "--state",
            str(self.prep.state),
            *args,
        ]
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.dict(os.environ, {"RESIDENT_TAG": ambient}),
            mock.patch.object(module, "Http", return_value=self.api),
            mock.patch.object(Preparation, "create", side_effect=self.api.create),
            mock.patch("sys.stdout", new_callable=io.StringIO) as output,
        ):
            module.main()
        return json.loads(output.getvalue())

    def test_every_persisted_profile_refreshes_pair_without_extra_loads_and_binds_receipt(
        self,
    ):
        for resident, required in self.REQUIRED.items():
            with self.subTest(resident=resident):
                self.api = FakeOllama()
                for tag in self.PAIR:
                    self.api.create(tag, self.posture.recipe(tag))
                previous = self.api.tags()
                for tag in self.PAIR:
                    recipe = self.posture.recipe(tag)
                    shown = json.loads(recipe.read_text())
                    shown["template"] = "current recipe for " + resident
                    recipe.write_text(json.dumps(shown))
                self.data["resident"] = resident
                self.config.write_text(json.dumps(self.data))
                self.make_preparation()
                runtime = FakeRuntime()
                receipt = self.prep.warm(runtime)
                expected_loaded = (
                    [resident, "gemma4:vlm"]
                    if resident == "qwen2.5-coder:fim"
                    else [resident]
                )
                self.assertEqual(set(receipt["models"]), required)
                self.assertEqual(
                    [row["name"] for row in self.api.running], expected_loaded
                )
                self.assertEqual(self.prep.verify(runtime)["status"], "verified")
                for tag in self.PAIR:
                    self.assertNotEqual(previous[tag], receipt["models"][tag]["digest"])
                    self.assertEqual(
                        receipt["models"][tag]["digest"], self.api.tags()[tag]
                    )
                # Pair tags remain acceptance obligations even when not loaded.
                tag = (
                    next(tag for tag in self.PAIR if tag not in expected_loaded)
                    if resident != "qwen2.5-coder:fim"
                    else "gemma4:vlm"
                )
                saved = self.api.models.pop(tag)
                with self.assertRaisesRegex(PreparationError, "identity"):
                    self.prep.verify(runtime)
                self.api.models[tag] = saved
                del receipt["models"][tag]
                (self.prep.state / "verified.json").write_text(json.dumps(receipt))
                with self.assertRaisesRegex(PreparationError, "all required models"):
                    self.prep.verify(runtime)

    def test_explicit_cli_preparation_selects_each_profile_without_loading_or_persisting(
        self,
    ):
        original = self.config.read_bytes()
        for resident, required in self.REQUIRED.items():
            with self.subTest(resident=resident):
                self.api = FakeOllama()
                result = self.cli("--resident", resident, "prepare")
                self.assertEqual(set(result), required)
                self.assertEqual(self.api.running, [])
                self.assertFalse((self.prep.state / "verified.json").exists())
                self.assertEqual(self.config.read_bytes(), original)

    def test_pair_command_refreshes_current_recipes_without_loading_for_every_profile(
        self,
    ):
        for resident in self.REQUIRED:
            with self.subTest(resident=resident):
                self.api = FakeOllama()
                original = self.cli("--resident", resident, "pair")
                for tag in self.PAIR:
                    recipe = self.posture.recipe(tag)
                    shown = json.loads(recipe.read_text())
                    shown["template"] = "pair refresh for " + resident
                    recipe.write_text(json.dumps(shown))
                result = self.cli("--resident", resident, "pair")
                self.assertEqual(set(result), self.PAIR)
                self.assertTrue(
                    all(
                        result[tag]["digest"] != original[tag]["digest"]
                        for tag in self.PAIR
                    )
                )
                self.assertEqual(self.api.running, [])
                self.assertFalse((self.prep.state / "verified.json").exists())

    def test_ambient_resident_cannot_change_prepare_or_render_check(self):
        root = self.root / "rendered"
        expected = self.posture.revision
        for ambient in ("qwen2.5-coder:fim", "not-a-profile"):
            with self.subTest(ambient=ambient):
                self.assertEqual(
                    set(self.cli("prepare", ambient=ambient)),
                    self.REQUIRED["gemma4:vlm"],
                )
                self.assertEqual(
                    self.cli("render", "--root", str(root), ambient=ambient)[
                        "revision"
                    ],
                    expected,
                )
                self.assertEqual(
                    self.cli("render", "--root", str(root), "--check", ambient=ambient)[
                        "changed"
                    ],
                    [],
                )
                self.assertEqual(self.api.running, [])

    def test_explicit_profile_render_is_temporary_and_default_check_detects_difference(
        self,
    ):
        original = self.config.read_bytes()
        root = self.root / "explicit-render"
        expected = Posture(self.config, self.recipes, "qwen2.5-coder:fim")
        result = self.cli(
            "--resident", "qwen2.5-coder:fim", "render", "--root", str(root)
        )
        self.assertEqual(result["revision"], expected.revision)
        self.assertIn(
            "OLLAMA_MAX_LOADED_MODELS=2",
            (root / "llama/host/ollama.service.d/homelab.conf").read_text(),
        )
        self.assertEqual(
            self.cli(
                "--resident",
                "qwen2.5-coder:fim",
                "render",
                "--root",
                str(root),
                "--check",
            )["changed"],
            [],
        )
        with self.assertRaisesRegex(PreparationError, "artifacts are stale"):
            self.cli("render", "--root", str(root), "--check")
        self.assertEqual(self.config.read_bytes(), original)

    def test_files_check_ignores_ambient_profile_without_any_compilation(self):
        installed = self.root / "installed"
        installed.mkdir()
        (installed / "model-posture.json").write_bytes(self.config.read_bytes())
        (installed / "modelfiles").mkdir()
        for recipe in self.recipes.iterdir():
            (installed / "modelfiles" / recipe.name).write_bytes(recipe.read_bytes())
        result = self.cli("files-check", "--installed", str(installed))
        self.assertEqual(result["status"], "unchanged effective posture")
        self.assertEqual(self.api.events, [])


class SafeCreationTests(FixtureCase):
    # Kill at filesystem boundaries, including the older Path.open route. The
    # assertions concern the abandoned artifacts and two successful warm retries,
    # not which Python creation API the implementation chooses.
    CREATOR = textwrap.dedent("""
        import os, pathlib, signal, sys
        sys.path.insert(0, sys.argv[1])
        import ollama_prepare as m
        root, fresh = pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3])
        boundary, mask = sys.argv[4], int(sys.argv[5], 8)
        state = fresh / 'parent' / 'state'
        target = fresh / 'parent' / 'output' / 'result.json'
        def kill(): os.kill(os.getpid(), signal.SIGKILL)
        def created(path):
            path = pathlib.Path(path)
            selected = {
                'ancestor': fresh,
                'state': state,
                'lock': state / 'preparation.lock',
                'intents': state / 'candidate-intents',
                'staging': state / 'candidate-intents' / m.STAGING_DIRECTORY,
                'publication-ancestor': fresh,
                'publication-directory': target.parent,
            }
            if path == selected.get(boundary): kill()
            if boundary == 'snapshot' and path.name.startswith('.ollama-preparing-'): kill()
            if boundary == 'staging-file' and path.suffix == '.tmp': kill()
        mkdir, opened, path_open = pathlib.Path.mkdir, os.open, pathlib.Path.open
        chmod, fchmod = os.chmod, os.fchmod
        paths = {}
        def make(self, *args, **kwargs):
            existed = self.exists()
            result = mkdir(self, *args, **kwargs)
            if not existed: created(self)
            return result
        def open_file(path, *args, **kwargs):
            existed = pathlib.Path(path).exists()
            fd = opened(path, *args, **kwargs)
            paths[fd] = pathlib.Path(path)
            if not existed: created(path)
            return fd
        def open_path(self, *args, **kwargs):
            existed = self.exists()
            out = path_open(self, *args, **kwargs)
            if not existed: created(self)
            return out
        def change_mode(path, *args, **kwargs):
            if boundary == 'before-state-chmod' and pathlib.Path(path) == state: kill()
            return chmod(path, *args, **kwargs)
        def change_fd_mode(fd, *args, **kwargs):
            path = paths.get(fd, pathlib.Path('/unrelated'))
            if boundary == 'before-lock-chmod' and path.name == 'preparation.lock': kill()
            if boundary == 'before-staging-chmod' and path.suffix == '.tmp': kill()
            return fchmod(fd, *args, **kwargs)
        pathlib.Path.mkdir, pathlib.Path.open = make, open_path
        os.open, os.chmod, os.fchmod = open_file, change_mode, change_fd_mode
        os.umask(mask)
        if boundary.startswith('publication-'):
            m.atomic_write(target, 'complete publication')
        else:
            class Api:
                url = 'http://fixture.invalid'
                def tags(self): return {}
                def call(self, path, **kwargs): return {'version': '0.40.11'}
            posture = m.Posture(root / 'model-posture.json', root / 'modelfiles')
            def no_create(*args): raise AssertionError('creation boundary not reached')
            prep = m.Preparation(posture, Api(), state, no_create)
            with prep.locked():
                prep.prepare()
        raise AssertionError('SIGKILL boundary not reached')
    """)

    def kill_creation(self, boundary, mask):
        fresh = self.root / f"generated-{boundary}-{mask:o}"
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                self.CREATOR,
                str(HOST),
                str(self.root),
                str(fresh),
                boundary,
                f"{mask:o}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, -signal.SIGKILL, result.stderr)
        for path in [fresh, *fresh.rglob("*")]:
            self.assertEqual(path.lstat().st_mode & 0o022, 0, str(path))
        return fresh

    def assert_warm_replay(self, fresh):
        prep = Preparation(
            self.posture, self.api, fresh / "parent/state", self.api.create
        )
        for _ in range(2):
            runtime = FakeRuntime()
            with prep.locked():
                receipt = prep.warm(runtime)
                self.assertEqual(prep.verify(runtime)["status"], "verified")
            self.assertEqual(receipt["revision"], self.posture.revision)
            self.assertEqual(runtime.stops, 0)
        self.assertEqual(list((prep.state / "candidate-intents").iterdir()), [])
        self.assertFalse(list(self.recipes.glob(".ollama-preparing-*.Modelfile")))
        self.assert_no_candidates()

    def test_killed_snapshot_creation_is_safe_before_any_permission_adjustment(self):
        for mask in (0o000, 0o002, 0o022, 0o077):
            with self.subTest(umask=f"{mask:04o}"):
                fresh = self.kill_creation("snapshot", mask)
                intent = next((fresh / "parent/state/candidate-intents").glob("*.json"))
                snapshot = Path(json.loads(intent.read_text())["snapshot"])
                self.assertTrue(snapshot.exists())
                self.assertEqual(snapshot.stat().st_mode & 0o777, 0o600)
                self.assert_warm_replay(fresh)
                self.assertFalse(snapshot.exists())

    def test_killed_owned_directories_locks_and_staging_replay_under_common_umasks(
        self,
    ):
        for mask in (0o000, 0o002, 0o022, 0o077):
            for boundary in (
                "ancestor",
                "state",
                "lock",
                "intents",
                "staging",
                "staging-file",
                "before-lock-chmod",
                "before-state-chmod",
                "before-staging-chmod",
            ):
                with self.subTest(boundary=boundary, umask=f"{mask:04o}"):
                    fresh = self.kill_creation(boundary, mask)
                    self.assert_warm_replay(fresh)

    def test_killed_publication_parent_creation_retries_under_common_umasks(self):
        for mask in (0o000, 0o002, 0o022, 0o077):
            for boundary in ("publication-ancestor", "publication-directory"):
                with self.subTest(boundary=boundary, umask=f"{mask:04o}"):
                    fresh = self.kill_creation(boundary, mask)
                    target = fresh / "parent/output/result.json"
                    for _ in range(2):
                        module.atomic_write(target, "complete publication")
                        self.assertEqual(target.read_text(), "complete publication")
                    self.assertEqual(target.stat().st_mode & 0o777, 0o644)
                    self.assertFalse(
                        (target.parent / module.STAGING_DIRECTORY).exists()
                    )

    def test_existing_untrusted_state_is_rejected_without_permission_repair(self):
        for path in (
            self.prep.state,
            self.prep.state / "preparation.lock",
            self.prep.intent_directory(),
        ):
            if not path.exists():
                path.touch()
            previous = path.stat().st_mode & 0o777
            try:
                path.chmod(previous | 0o020)
                with self.assertRaisesRegex(PreparationError, "group/world-writable"):
                    self.prep.replay_candidates()
                self.assertEqual(path.stat().st_mode & 0o777, previous | 0o020)
            finally:
                path.chmod(previous)

    def test_existing_generic_publication_parent_retains_caller_permissions(self):
        parent = self.root / "shared-render-directory"
        parent.mkdir()
        parent.chmod(0o775)
        target = parent / "generated.conf"
        module.atomic_write(target, "complete publication")
        self.assertEqual(target.read_text(), "complete publication")
        self.assertEqual(parent.stat().st_mode & 0o777, 0o775)


class AtomicPublicationTests(FixtureCase):
    WRITER = textwrap.dedent("""
        import os, pathlib, signal, stat, sys, time
        sys.path.insert(0, sys.argv[1])
        import ollama_prepare as m
        target, stage, content = pathlib.Path(sys.argv[2]), sys.argv[3], sys.argv[4]
        def kill(): os.kill(os.getpid(), signal.SIGKILL)
        mkdir, replace, fsync, fdopen = pathlib.Path.mkdir, pathlib.Path.replace, os.fsync, os.fdopen
        def make(self, *args, **kwargs):
            result = mkdir(self, *args, **kwargs)
            if stage == 'mkdir' and self.name == m.STAGING_DIRECTORY: kill()
            return result
        def commit(self, destination):
            if stage == 'before-rename': kill()
            if stage == 'pause':
                pathlib.Path(sys.argv[5]).touch()
                deadline = time.monotonic() + 10
                while not pathlib.Path(sys.argv[6]).exists():
                    if time.monotonic() > deadline: raise RuntimeError('release timed out')
                    time.sleep(.01)
            result = replace(self, destination)
            if stage == 'after-rename': kill()
            return result
        def sync(fd):
            result = fsync(fd)
            if stage == 'file-fsync' and stat.S_ISREG(os.fstat(fd).st_mode): kill()
            return result
        class PartialWrite:
            def __init__(self, out): self.out = out
            def __enter__(self): return self
            def __exit__(self, *args): return self.out.__exit__(*args)
            def write(self, content):
                self.out.write(content[:max(1, len(content)//2)])
                self.out.flush()
                kill()
            def __getattr__(self, name): return getattr(self.out, name)
        def opened(*args, **kwargs):
            out = fdopen(*args, **kwargs)
            return PartialWrite(out) if stage == 'write' else out
        pathlib.Path.mkdir, pathlib.Path.replace = make, commit
        m.os.fsync, m.os.fdopen = sync, opened
        m.atomic_write(target, content)
    """)

    def killed_write(self, target, stage, content):
        result = subprocess.run(
            [sys.executable, "-c", self.WRITER, str(HOST), str(target), stage, content],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, -signal.SIGKILL, result.stderr)

    def test_intent_crash_stages_replay_without_using_staging_as_authority(self):
        directory = self.prep.intent_directory()
        for number, stage in enumerate(
            ["mkdir", "write", "file-fsync", "before-rename", "after-rename"]
        ):
            with self.subTest(stage=stage):
                token = f"{number + 1:032x}"
                tag = "homelab-preparing/fixture:" + token
                snapshot = self.recipes / (".ollama-preparing-" + token + ".Modelfile")
                snapshot.write_text(
                    "operator bytes that only a committed intent may delete"
                )
                self.api.models[tag] = {"digest": "a" * 64, "show": {}}
                intent = {"schema": 1, "tag": tag, "snapshot": str(snapshot)}
                self.killed_write(
                    directory / (token + ".json"), stage, json.dumps(intent)
                )
                with self.prep.locked():
                    self.prep.replay_candidates()
                self.assertEqual(list(directory.iterdir()), [])
                if stage == "after-rename":
                    self.assertNotIn(tag, self.api.tags())
                    self.assertFalse(snapshot.exists())
                else:
                    self.assertIn(tag, self.api.tags())
                    self.assertTrue(snapshot.exists())
                # Both retries were permanently blocked in the original probe.
                for _ in range(2):
                    with self.prep.locked():
                        self.prep.prepare()

    def test_receipt_crash_preserves_only_completed_rename(self):
        receipt = self.prep.warm(FakeRuntime())
        target = self.prep.state / "verified.json"
        for stage in ["mkdir", "write", "file-fsync", "before-rename", "after-rename"]:
            with self.subTest(stage=stage):
                target.unlink(missing_ok=True)
                self.killed_write(target, stage, json.dumps(receipt))
                with self.prep.locked():
                    if stage == "after-rename":
                        self.assertEqual(
                            self.prep.verify(FakeRuntime())["status"], "verified"
                        )
                    else:
                        with self.assertRaises(PreparationError):
                            self.prep.verify(FakeRuntime())
                self.assertFalse((self.prep.state / module.STAGING_DIRECTORY).exists())

    def test_render_reclaims_staging_even_when_renamed_output_is_already_current(self):
        root = self.root / "rendered"
        target = root / "llama/host/ollama.service.d/homelab.conf"
        for stage in ["write", "before-rename", "after-rename"]:
            with self.subTest(stage=stage):
                self.killed_write(target, stage, module.dropin(self.posture))
                module.render(self.posture, root, check=False)
                module.render(self.posture, root, check=True)
                self.assertFalse((target.parent / module.STAGING_DIRECTORY).exists())

    def test_staging_schema_reclaims_opaque_bytes_without_parsing(self):
        staging = self.prep.intent_directory() / module.STAGING_DIRECTORY
        staging.mkdir(mode=0o700)
        temporary = staging / ("a" * 32 + ".tmp")
        temporary.write_text('{"tag":"operator:keep","snapshot":"/unrelated"')
        self.api.models["operator:keep"] = {"digest": "b" * 64, "show": {}}
        self.prep.replay_candidates()
        self.assertFalse(staging.exists())
        self.assertIn("operator:keep", self.api.tags())
        self.assertEqual(self.api.events, [])

    def test_staging_symlinks_permissions_and_unknown_names_are_not_reclaimed(self):
        directory = self.prep.intent_directory()
        staging = directory / module.STAGING_DIRECTORY
        elsewhere = self.root / "keep"
        elsewhere.mkdir()
        keep = elsewhere / ("a" * 32 + ".tmp")
        keep.write_text("keep")
        staging.symlink_to(elsewhere, target_is_directory=True)
        with self.assertRaisesRegex(PreparationError, "staging directory"):
            self.prep.replay_candidates()
        self.assertTrue(keep.exists())
        staging.unlink()
        staging.mkdir(mode=0o700)
        temporary = staging / keep.name
        temporary.symlink_to(keep)
        with self.assertRaisesRegex(PreparationError, "staging file"):
            self.prep.replay_candidates()
        temporary.unlink()
        temporary.write_text("opaque")
        temporary.chmod(0o666)
        with self.assertRaisesRegex(PreparationError, "staging file"):
            self.prep.replay_candidates()
        temporary.unlink()
        unknown = staging / "operator.json"
        unknown.write_text("{}")
        with self.assertRaisesRegex(
            PreparationError, "unexpected atomic write staging"
        ):
            self.prep.replay_candidates()
        self.assertTrue(unknown.exists())

    def test_staging_directory_ownership_and_write_permissions_are_checked(self):
        staging = self.prep.intent_directory() / module.STAGING_DIRECTORY
        staging.mkdir(mode=0o700)
        temporary = staging / ("a" * 32 + ".tmp")
        temporary.write_text("opaque")
        staging.chmod(0o777)
        with self.assertRaisesRegex(PreparationError, "staging directory"):
            self.prep.replay_candidates()
        staging.chmod(0o700)
        lstat = Path.lstat

        def wrong_owner(path, *args, **kwargs):
            info = lstat(path, *args, **kwargs)
            if path == staging:
                fields = list(info)
                fields[4] = os.geteuid() + 1
                return os.stat_result(fields)
            return info

        with mock.patch.object(Path, "lstat", wrong_owner):
            with self.assertRaisesRegex(PreparationError, "staging directory"):
                self.prep.replay_candidates()
        self.assertTrue(temporary.exists())

    def test_active_writer_and_reclaimer_share_directory_lock(self):
        target = self.root / "publication" / "verified.json"
        ready, release = self.root / "writer-ready", self.root / "writer-release"
        worker = subprocess.Popen(
            [
                sys.executable,
                "-c",
                self.WRITER,
                str(HOST),
                str(target),
                "pause",
                "complete",
                str(ready),
                str(release),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        done, errors = threading.Event(), []

        def reclaim():
            try:
                module.recover_atomic_writes(target.parent)
            except BaseException as exc:
                errors.append(exc)
            finally:
                done.set()

        thread = threading.Thread(target=reclaim)
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready.exists(), "writer did not reach controlled commit")
            thread.start()
            self.assertFalse(
                done.wait(0.1), "reclaimer entered the active writer's staging"
            )
            release.touch()
            out, err = worker.communicate(timeout=5)
            self.assertEqual(worker.returncode, 0, out + err)
            self.assertTrue(done.wait(5))
            self.assertEqual(errors, [])
            self.assertEqual(target.read_text(), "complete")
            self.assertFalse((target.parent / module.STAGING_DIRECTORY).exists())
        finally:
            release.touch()
            if worker.poll() is None:
                worker.kill()
            worker.communicate(timeout=5)
            if thread.ident is not None:
                thread.join(timeout=5)

    def test_publication_and_invalidation_sync_parent_after_namespace_change(self):
        events = []
        replace, unlink, fsync = Path.replace, Path.unlink, os.fsync
        target = self.prep.state / "verified.json"

        def renamed(path, destination):
            result = replace(path, destination)
            events.append("rename")
            return result

        def removed(path, *args, **kwargs):
            result = unlink(path, *args, **kwargs)
            if path == target:
                events.append("unlink")
            return result

        def synced(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                events.append("directory fsync")
            return fsync(fd)

        with (
            mock.patch.object(Path, "replace", renamed),
            mock.patch.object(Path, "unlink", removed),
            mock.patch.object(module.os, "fsync", synced),
        ):
            module.atomic_write(target, "complete")
            self.assertIn("directory fsync", events[events.index("rename") + 1 :])
            events.clear()
            self.prep.invalidate_receipt()
            self.assertIn("directory fsync", events[events.index("unlink") + 1 :])


class CancellationBoundaryTests(FixtureCase):
    def test_pre_adoption_cancellation_preserves_acceptance_and_daemon(self):
        import ollama_runtime

        for phase in ("lock", "capture"):
            with self.subTest(phase=phase):
                self.prep.warm(FakeRuntime())
                receipt = (self.prep.state / "verified.json").read_bytes()
                runtime = FakeRuntime()
                target = (
                    mock.patch.object(
                        module, "bounded_flock", side_effect=KeyboardInterrupt
                    )
                    if phase == "lock"
                    else mock.patch.object(
                        ollama_runtime, "Runtime", side_effect=KeyboardInterrupt
                    )
                )
                with self.cli(runtime), target:
                    with self.assertRaises(KeyboardInterrupt):
                        module.main()
                self.assertEqual(
                    (self.prep.state / "verified.json").read_bytes(), receipt
                )
                self.assertEqual(runtime.stops, 0)

    @contextmanager
    def cli(self, runtime, command="warm", output=None):
        import ollama_runtime
        import ollama_signals

        runtime.closed = False

        @contextmanager
        def factory():
            try:
                yield runtime
            finally:
                runtime.closed = True

        stop = runtime.stop_failed

        def stopped():
            self.assertFalse(
                runtime.closed, "runtime closed before cancellation cleanup"
            )
            return stop()

        argv = [
            "ollama_prepare.py",
            "--config",
            str(self.config),
            "--recipes",
            str(self.recipes),
            "--state",
            str(self.prep.state),
            *command.split(),
        ]
        previous = {
            signum: signal.getsignal(signum)
            for signum in (signal.SIGTERM, signal.SIGINT)
        }
        previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, set())
        try:
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(module, "Http", return_value=self.api),
                mock.patch.object(Preparation, "create", side_effect=self.api.create),
                mock.patch.object(ollama_runtime, "Runtime", factory),
                mock.patch.object(runtime, "stop_failed", side_effect=stopped),
                mock.patch("sys.stdout", output or io.StringIO()),
                mock.patch.object(
                    ollama_signals._state, "retained", False, create=True
                ),
            ):
                yield
        finally:
            # Only fixtures restore process state. The real adopting CLI owns
            # these signals through exit and deliberately has no teardown.
            signal.pthread_sigmask(signal.SIG_BLOCK, module.CommandCancellation.SIGNALS)
            pending = signal.sigpending() & module.CommandCancellation.SIGNALS
            while pending:
                signal.sigwait(pending)
                pending = signal.sigpending() & module.CommandCancellation.SIGNALS
            for signum, handler in previous.items():
                signal.signal(signum, handler)
            pending = signal.sigpending() & module.CommandCancellation.SIGNALS
            while pending:
                signal.sigwait(pending)
                pending = signal.sigpending() & module.CommandCancellation.SIGNALS
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)

    def test_response_write_and_flush_cancellation_are_adoption_failures(self):
        for command in ("warm", "verify --stop-on-failure"):
            for phase in ("write", "flush"):
                with self.subTest(command=command, phase=phase):
                    if command.startswith("verify"):
                        self.prep.warm(FakeRuntime())

                    class Output(io.StringIO):
                        fired = False

                        def write(inner, value):
                            if phase == "write" and not inner.fired:
                                inner.fired = True
                                os.kill(os.getpid(), signal.SIGTERM)
                            return super().write(value)

                        def flush(inner):
                            if phase == "flush" and not inner.fired:
                                inner.fired = True
                                os.kill(os.getpid(), signal.SIGINT)
                            return super().flush()

                    runtime, output = FakeRuntime(), Output()
                    with self.cli(runtime, command, output):
                        with self.assertRaisesRegex(
                            PreparationError, "captured daemon stopped"
                        ):
                            module.main()
                    self.assertTrue(output.fired)
                    self.assertEqual(runtime.stops, 1)
                    self.assertFalse((self.prep.state / "verified.json").exists())

    def test_plain_verification_cancellation_does_not_invalidate_or_stop(self):
        self.prep.warm(FakeRuntime())
        receipt = (self.prep.state / "verified.json").read_bytes()
        for where in ("verify", "response"):
            with self.subTest(where=where):
                runtime = FakeRuntime()

                class Output(io.StringIO):
                    def flush(inner):
                        if where == "response":
                            raise InterruptedError("observational response cancelled")

                with (
                    self.cli(runtime, "verify", Output()),
                    mock.patch.object(module, "CommandCancellation") as managed,
                ):
                    if where == "verify":
                        runtime.verify = mock.Mock(
                            side_effect=KeyboardInterrupt(
                                "observational check cancelled"
                            )
                        )
                    with self.assertRaises((PreparationError, InterruptedError)):
                        module.main()
                    managed.assert_not_called()
                self.assertEqual(runtime.stops, 0)
                self.assertEqual(
                    (self.prep.state / "verified.json").read_bytes(), receipt
                )

    def test_repeated_signals_during_abort_cannot_skip_stop(self):
        runtime = FakeRuntime(error=True)
        tags = self.api.tags
        delivered = False

        def inventory():
            nonlocal delivered
            if self.prep._aborting and not delivered:
                delivered = True
                blocked = signal.pthread_sigmask(signal.SIG_BLOCK, set())
                self.assertTrue({signal.SIGTERM, signal.SIGINT} <= blocked)
                os.kill(os.getpid(), signal.SIGTERM)
                os.kill(os.getpid(), signal.SIGINT)
            return tags()

        with (
            self.cli(runtime),
            mock.patch.object(self.api, "tags", side_effect=inventory),
        ):
            cancellation = module.CommandCancellation(lambda: self.prep._aborting)
            cancellation.activate()
            with self.assertRaisesRegex(PreparationError, "captured daemon stopped"):
                self.prep.warm(runtime)
        self.assertTrue(delivered)
        self.assertEqual(runtime.stops, 1)
        self.assertFalse((self.prep.state / "verified.json").exists())

    def test_invalidation_and_diagnostic_failures_still_attempt_stop(self):
        runtime = FakeRuntime()
        broken = mock.Mock()
        broken.write.side_effect = OSError("closed stderr")
        with (
            mock.patch.object(
                self.api, "tags", side_effect=KeyboardInterrupt("cleanup cancelled")
            ),
            mock.patch("sys.stderr", broken),
        ):
            with self.assertRaisesRegex(
                PreparationError, "captured daemon stopped.*INVALIDATION FAILED"
            ):
                self.prep.abort(runtime, RuntimeError("original failure"))
        self.assertEqual(runtime.stops, 1)

    def test_failed_receipt_parent_sync_invalidates_publication_and_stops(self):
        runtime, failed = FakeRuntime(), False
        sync = module.sync_directory

        def interrupted_sync(path):
            nonlocal failed
            if (
                not failed
                and Path(path) == self.prep.state
                and (self.prep.state / "verified.json").exists()
            ):
                failed = True
                raise OSError("receipt parent sync failed")
            return sync(path)

        with mock.patch.object(module, "sync_directory", side_effect=interrupted_sync):
            with self.assertRaisesRegex(
                PreparationError, "captured daemon stopped.*receipt parent sync failed"
            ):
                self.prep.warm(runtime)
        self.assertTrue(failed)
        self.assertEqual(runtime.stops, 1)
        self.assertFalse((self.prep.state / "verified.json").exists())


class ResourceCancellationTests(FixtureCase):
    @staticmethod
    def run_probe(action, boundary, signum):
        import ollama_signals as scopes

        case = FixtureCase()
        case.setUp()
        prep, runtime = case.prep, FakeRuntime()
        receipt = prep.state / "verified.json"
        receipt.write_text("old acceptance")
        controller = module.CommandCancellation(lambda: prep._aborting)
        prep.cancellation = controller
        result = {"fired": False, "transitions": [], "error": None}
        descriptors = []
        real_open, real_close, real_fdopen = os.open, os.close, os.fdopen
        real_mask = signal.pthread_sigmask
        keeper = []

        def fire(label):
            if label == boundary and not result["fired"]:
                result["fired"] = True
                os.kill(os.getpid(), signum)
                # A second cancellation must not unwind resource release/abort.
                os.kill(
                    os.getpid(),
                    signal.SIGINT if signum == signal.SIGTERM else signal.SIGTERM,
                )

        def opened(*args, **kwargs):
            fd = real_open(*args, **kwargs)
            descriptors.append(fd)
            return fd

        class Stream:
            def __init__(self, out):
                self.out = out

            def __enter__(self):
                fire("stream-enter")
                self.out.__enter__()
                return self

            def __exit__(self, *exc):
                fire("stream-exit")
                return self.out.__exit__(*exc)

            def __getattr__(self, key):
                return getattr(self.out, key)

        def mask(how, values):
            index = len(result["transitions"])
            result["transitions"].append([int(how), sorted(values)])
            fire(f"mask-{index}-before")
            value = real_mask(how, values)
            fire(f"mask-{index}-after")
            return value

        def trace(frame, event, _arg):
            if event == "call" and frame.f_code.co_name in ("__enter__", "__exit__"):
                manager = frame.f_locals.get("self")
                generator = getattr(manager, "gen", None)
                name = "locked" if action == "transaction" else "publication_directory"
                if generator is not None and generator.gi_code.co_name == name:
                    layer = (
                        "outer"
                        if isinstance(manager, scopes._DeferredContext)
                        else "inner"
                    )
                    fire(f"{layer}-{frame.f_code.co_name}")
            if frame.f_code in (
                module.sync_directory.__wrapped__.__code__,
                module.write_snapshot.__wrapped__.__code__,
            ):
                fire(f"{frame.f_code.co_name}-{event}")
            return trace

        def work():
            if action == "publication":
                with module.publication_directory(prep.state):
                    pass
            elif action == "receipt":
                module.atomic_write(receipt, "new acceptance")
            elif action == "intent":
                token = "a" * 32
                snapshot = case.recipes / f".ollama-preparing-{token}.Modelfile"
                module.atomic_write(
                    prep.intent_directory() / f"{token}.json",
                    json.dumps(
                        {
                            "schema": 1,
                            "tag": f"homelab-preparing/fixture:{token}",
                            "snapshot": str(snapshot),
                        }
                    ),
                )
            elif action == "recovery":
                module.recover_atomic_writes(prep.state)
            elif action == "snapshot":
                module.write_snapshot(
                    case.recipes / "snapshot.Modelfile", b"captured bytes"
                )
            elif action == "sync":
                module.sync_directory(prep.state)
            elif action == "transaction":
                with prep.locked():
                    prep.checkpoint()
            else:
                raise AssertionError(action)

        controller.activate()
        started = time.monotonic()
        with (
            mock.patch.object(os, "open", opened),
            mock.patch.object(
                os,
                "fdopen",
                side_effect=lambda *args, **kwargs: Stream(
                    real_fdopen(*args, **kwargs)
                ),
            ),
            mock.patch.object(signal, "pthread_sigmask", mask),
        ):
            sys.settrace(trace)
            try:
                with prep.adopting(runtime):
                    work()
                    prep.checkpoint()
                    raise PreparationError("end-of-probe cleanup")
            except BaseException as exc:
                keeper.append(
                    exc
                )  # Retain traceback: GC must not release locks for us.
                result["error"] = str(exc)
            finally:
                sys.settrace(None)
                controller.quiesce(terminal=True)
        result["elapsed"] = time.monotonic() - started
        result["stops"] = runtime.stops
        result["receipt"] = receipt.exists()
        result["leaked_descriptors"] = []
        for fd in set(descriptors):
            try:
                os.fstat(fd)
            except OSError as exc:
                assert exc.errno == errno.EBADF
            else:
                result["leaked_descriptors"].append(fd)
        for path, flags in (
            (prep.state, os.O_RDONLY),
            (prep.state / "preparation.lock", os.O_RDWR | os.O_CREAT),
        ):
            fd = real_open(path, flags, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                real_close(fd)
        print(json.dumps(result), flush=True)
        case.doCleanups()

    def probe(self, action, boundary, signum):
        script = textwrap.dedent("""\
            import importlib.util,sys
            spec=importlib.util.spec_from_file_location('fixtures',sys.argv[1])
            tests=importlib.util.module_from_spec(spec);spec.loader.exec_module(tests)
            tests.ResourceCancellationTests.run_probe(sys.argv[2],sys.argv[3],int(sys.argv[4]))
        """)
        child = subprocess.run(
            [sys.executable, "-c", script, __file__, action, boundary, str(signum)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env={**os.environ, "TMPDIR": str(self.root)},
        )
        self.assertEqual(child.returncode, 0, (child.stdout, child.stderr))
        result = json.loads(child.stdout.splitlines()[-1])
        if boundary != "observe":
            self.assertTrue(result["fired"], result)
        self.assertEqual(result["stops"], 1, result)
        self.assertFalse(result["receipt"], result)
        self.assertEqual(result["leaked_descriptors"], [], result)
        self.assertNotIn("INVALIDATION FAILED", result["error"])
        self.assertLess(result["elapsed"], 2, result)
        return result

    def test_real_signals_at_both_layers_of_resource_context_entry_and_exit(self):
        for action in ("publication", "receipt", "intent", "recovery", "transaction"):
            for layer in ("inner", "outer"):
                for edge in ("__enter__", "__exit__"):
                    for signum in (signal.SIGTERM, signal.SIGINT):
                        with self.subTest(
                            action=action, layer=layer, edge=edge, signal=signum
                        ):
                            self.probe(action, f"{layer}-{edge}", signum)

    def test_real_signals_cover_file_stream_and_fsync_ownership(self):
        for action, boundaries in (
            ("receipt", ("stream-enter", "stream-exit")),
            (
                "snapshot",
                (
                    "stream-enter",
                    "stream-exit",
                    "write_snapshot-call",
                    "write_snapshot-return",
                ),
            ),
            ("sync", ("sync_directory-call", "sync_directory-return")),
        ):
            for boundary in boundaries:
                for signum in (signal.SIGTERM, signal.SIGINT):
                    with self.subTest(action=action, boundary=boundary, signal=signum):
                        self.probe(action, boundary, signum)

    def test_real_signals_before_and_after_every_resource_mask_transition(self):
        baseline = self.probe("receipt", "observe", signal.SIGTERM)
        for index in range(len(baseline["transitions"])):
            for side in ("before", "after"):
                for signum in (signal.SIGTERM, signal.SIGINT):
                    with self.subTest(index=index, side=side, signal=signum):
                        self.probe("receipt", f"mask-{index}-{side}", signum)

    def test_contended_invalidation_is_bounded_and_still_attempts_stop(self):
        self.prep.state.joinpath("verified.json").write_text("old acceptance")
        holder = os.open(self.prep.state, os.O_RDONLY)
        try:
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
            runtime = FakeRuntime()
            started = time.monotonic()
            with mock.patch.object(module, "PUBLICATION_LOCK_TIMEOUT", 0.05):
                with self.assertRaisesRegex(
                    PreparationError,
                    "captured daemon stopped.*INVALIDATION FAILED.*publication directory lock",
                ):
                    self.prep.abort(runtime, RuntimeError("fixture failure"))
            self.assertLess(time.monotonic() - started, 1)
            self.assertEqual(runtime.stops, 1)
            self.assertTrue((self.prep.state / "verified.json").exists())
            runtime.stop_failed = mock.Mock(side_effect=OSError("stop unavailable"))
            with mock.patch.object(module, "PUBLICATION_LOCK_TIMEOUT", 0.05):
                with self.assertRaisesRegex(
                    PreparationError,
                    "STOP FAILED: stop unavailable.*INVALIDATION FAILED",
                ):
                    self.prep.abort(runtime, RuntimeError("fixture failure"))
            runtime.stop_failed.assert_called_once()
        finally:
            os.close(holder)

    def test_transaction_lock_wait_is_bounded_and_preserves_other_owner(self):
        lock = self.prep.state / "preparation.lock"
        with lock.open("a+") as holder:
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
            started = time.monotonic()
            with mock.patch.object(module, "PREPARATION_LOCK_TIMEOUT", 0.05):
                with self.assertRaisesRegex(
                    PreparationError, "preparation transaction lock"
                ):
                    with self.prep.locked():
                        self.fail("entered a contended transaction")
            self.assertLess(time.monotonic() - started, 1)
            self.assertIsNone(self.prep._lock_fd)
            with lock.open("a+") as contender:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_invalidation_budget_never_prevents_target_stop(self):
        runtime = FakeRuntime()
        with mock.patch.object(module, "INVALIDATION_BUDGET", 0):
            with self.assertRaisesRegex(
                PreparationError,
                "captured daemon stopped.*INVALIDATION FAILED.*time budget",
            ):
                self.prep.abort(runtime, RuntimeError("fixture failure"))
        self.assertEqual(runtime.stops, 1)

    def test_real_runtime_stop_observations_keep_cleanup_signals_masked(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "runtime_fixtures", HOST / "tests/test-ollama-runtime.py"
        )
        fixtures = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixtures)
        host = fixtures.Host(self.root / "proc-fixture")
        with host.runtime() as runtime:
            observed = []

            def command(argv):
                observed.append(signal.pthread_sigmask(signal.SIG_BLOCK, set()))
                return host.command(argv)

            runtime.run = command
            with self.assertRaisesRegex(PreparationError, "captured daemon stopped"):
                self.prep.abort(runtime, RuntimeError("fixture failure"))
            self.assertTrue(observed)
            for mask in observed:
                self.assertTrue({signal.SIGTERM, signal.SIGINT} <= mask)
            self.assertEqual(host.signals, [(1000, signal.SIGHUP, None, 0)])

    def test_failed_stream_adoption_closes_raw_descriptor(self):
        real_open = os.open
        for operation in ("receipt", "snapshot"):
            descriptors = []

            def opened(*args, **kwargs):
                fd = real_open(*args, **kwargs)
                descriptors.append(fd)
                return fd

            with (
                self.subTest(operation=operation),
                mock.patch.object(os, "open", opened),
                mock.patch.object(
                    os, "fdopen", side_effect=OSError("stream ownership failed")
                ),
            ):
                with self.assertRaisesRegex(OSError, "stream ownership failed"):
                    if operation == "receipt":
                        module.atomic_write(
                            self.prep.state / "verified.json", "fixture"
                        )
                    else:
                        module.write_snapshot(
                            self.recipes / "fixture.Modelfile", b"fixture"
                        )
            for fd in set(descriptors):
                with self.assertRaises(OSError) as failure:
                    os.fstat(fd)
                self.assertEqual(failure.exception.errno, errno.EBADF)


class CancellationOperationTests(FixtureCase):
    def test_recorded_cancellation_stops_before_next_model_operation(self):
        phases = (
            "create",
            "show",
            "copy",
            "candidate-cleanup",
            "load",
            "loaded",
            "runtime",
            "ready-retry",
        )
        for phase in phases:
            for signum in (signal.SIGTERM, signal.SIGINT):
                with self.subTest(phase=phase, signal=signum):
                    case = FixtureCase()
                    case.setUp()
                    self.addCleanup(case.doCleanups)
                    runtime = FakeRuntime()
                    owner, method = {
                        "create": (case.api, "create"),
                        "show": (case.api, "show"),
                        "copy": (case.api, "call"),
                        "candidate-cleanup": (case.api, "call"),
                        "load": (case.api, "load"),
                        "loaded": (case.api, "loaded"),
                        "runtime": (runtime, "verify"),
                        "ready-retry": (case.api, "call"),
                    }[phase]
                    original = getattr(owner, method)
                    observed = []

                    def wrapped(*args, **kwargs):
                        selected = not observed and (
                            phase not in ("copy", "candidate-cleanup", "ready-retry")
                            or args[0]
                            == {
                                "copy": "/api/copy",
                                "candidate-cleanup": "/api/delete",
                                "ready-retry": "/api/version",
                            }[phase]
                        )
                        if not selected:
                            return original(*args, **kwargs)
                        value = original(*args, **kwargs)
                        try:
                            raise OSError("internally handled adapter error")
                        except OSError:
                            os.kill(os.getpid(), signum)
                        observed.append(len(case.api.events))
                        if phase == "ready-retry":
                            raise TransportUnavailable("fixture transient connection")
                        return value

                    with mock.patch.object(owner, method, side_effect=wrapped):
                        with CancellationBoundaryTests.cli(case, runtime):
                            with self.assertRaisesRegex(
                                PreparationError, "adoption interrupted by signal"
                            ):
                                module.main()
                    self.assertEqual(len(observed), 1)
                    self.assertEqual(runtime.stops, 1)
                    self.assertFalse((case.prep.state / "verified.json").exists())
                    self.assertFalse(
                        any(
                            event[0] in ("create", "/api/copy", "load")
                            for event in case.api.events[observed[0] :]
                        ),
                        case.api.events,
                    )
                    # Any interrupted candidate cleanup remains owned/replayable;
                    # cancellation never uses an incomplete artifact as authority.
                    case.prep.replay_candidates()
                    self.assertFalse(
                        list((case.prep.state / "candidate-intents").glob("*.json"))
                    )
                    self.assertFalse(
                        list(case.recipes.glob(".ollama-preparing-*.Modelfile"))
                    )

    @staticmethod
    def run_create_probe(mode, signum):
        case = CancellationBoundaryTests()
        case.setUp()
        runtime = FakeRuntime()
        script = case.root / "slow-create.py"
        ready = case.root / "child-ready"
        script.write_text(
            "import os,sys,time\nfrom pathlib import Path\n"
            "Path(sys.argv[1]).write_text(str(os.getpid()))\n"
            "print('captured stdout', flush=True)\n"
            "print('captured stderr', file=sys.stderr, flush=True)\n"
            "time.sleep(5)\n"
        )
        real_run = subprocess.run
        real_create = Preparation.create
        result = {"creates": 0, "signals": 0, "swallowed": False}
        stop_sending = threading.Event()
        sender = None

        def send():
            deadline = time.monotonic() + 5
            while not ready.exists() and not stop_sending.wait(0.01):
                if time.monotonic() >= deadline:
                    return
            for _ in range(2 if mode == "swallow-first" else 1):
                if stop_sending.wait(0.1):
                    return
                result["signals"] += 1
                os.kill(os.getpid(), signum)

        def run(argv, **kwargs):
            nonlocal sender
            assert argv[:2] == ["ollama", "create"]
            assert kwargs["capture_output"] is True and kwargs["timeout"] == 900
            assert kwargs["pass_fds"]
            result["creates"] += 1
            if mode == "timeout":
                kwargs["timeout"] = 0.1
            else:
                sender = threading.Thread(target=send)
                sender.start()
            try:
                return real_run([sys.executable, str(script), str(ready)], **kwargs)
            except BaseException as exc:
                if mode != "swallow-first" or not isinstance(
                    exc, module.AdoptionCancelled
                ):
                    raise
                result["swallowed"] = True
            # Deliberately outside the except suite: the next signal must still
            # interrupt ordinary work after a badly behaved dependency swallowed
            # the first cancellation. This is still within one operation call.
            time.sleep(5)
            raise AssertionError("second cancellation did not interrupt work")

        started = time.monotonic()
        with CancellationBoundaryTests.cli(case, runtime):
            with (
                mock.patch.object(Preparation, "create", real_create),
                mock.patch.object(subprocess, "run", side_effect=run),
            ):
                try:
                    module.main()
                except BaseException as exc:
                    result["error"] = str(exc)
                finally:
                    stop_sending.set()
                    if sender:
                        sender.join(2)
                        assert not sender.is_alive()
        result["elapsed"] = time.monotonic() - started
        result["stops"] = runtime.stops
        result["receipt"] = (case.prep.state / "verified.json").exists()
        result["model_events"] = case.api.events
        result["intents"] = len(
            list((case.prep.state / "candidate-intents").glob("*.json"))
        )
        try:
            os.kill(int(ready.read_text()), 0)
        except ProcessLookupError:
            result["child_reaped"] = True
        else:
            result["child_reaped"] = False
        case.prep.replay_candidates()
        result["replayed"] = not list(
            (case.prep.state / "candidate-intents").glob("*.json")
        )
        result["snapshots"] = len(
            list(case.recipes.glob(".ollama-preparing-*.Modelfile"))
        )
        print(json.dumps(result), flush=True)
        case.doCleanups()

    def create_probe(self, mode, signum):
        script = textwrap.dedent("""\
            import importlib.util,sys
            spec=importlib.util.spec_from_file_location('fixtures',sys.argv[1])
            tests=importlib.util.module_from_spec(spec)
            spec.loader.exec_module(tests)
            tests.CancellationOperationTests.run_create_probe(sys.argv[2],int(sys.argv[3]))
        """)
        child = subprocess.run(
            [sys.executable, "-c", script, __file__, mode, str(signum)],
            capture_output=True,
            text=True,
            timeout=10,
            env={**os.environ, "TMPDIR": str(self.root)},
            check=False,
        )
        self.assertEqual(child.returncode, 0, (child.stdout, child.stderr))
        result = json.loads(child.stdout.splitlines()[-1])
        self.assertLess(result["elapsed"], 2, result)
        self.assertEqual(result["creates"], 1, result)
        self.assertEqual(result["stops"], 1, result)
        self.assertFalse(result["receipt"], result)
        self.assertEqual(result["model_events"], [], result)
        self.assertTrue(result["child_reaped"], result)
        self.assertEqual(result["intents"], 1, result)
        self.assertTrue(result["replayed"], result)
        self.assertEqual(result["snapshots"], 0, result)
        return result

    def test_pid_directed_cancellation_escapes_real_subprocess_selector(self):
        self.assertFalse(issubclass(module.AdoptionCancelled, Exception))
        for signum in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=signum):
                result = self.create_probe("cancel", signum)
                self.assertEqual(result["signals"], 1)
                self.assertIn("adoption interrupted by signal", result["error"])

    def test_later_signals_still_raise_after_dependency_swallows_first(self):
        for signum in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=signum):
                result = self.create_probe("swallow-first", signum)
                self.assertEqual(result["signals"], 2)
                self.assertTrue(result["swallowed"])

    def test_bounded_create_timeout_stops_and_explains_resumable_bootstrap_retry(self):
        result = self.create_probe("timeout", signal.SIGTERM)
        for guidance in (
            "900-second limit",
            "base-model download",
            "partial downloads",
            "Keep the model store",
            "bootstrap daemon",
            "--bootstrap-recovery",
            "same bounded limit",
        ):
            self.assertIn(guidance, result["error"])

    def test_preparation_timeout_preserves_daemon_and_bounds_retry_guidance(self):
        with (
            self.prep.locked(),
            mock.patch.object(
                module.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired("ollama create", 900),
            ) as run,
        ):
            with self.assertRaisesRegex(
                PreparationError,
                "does not stop the daemon",
            ) as failure:
                self.prep.create("fixture:candidate", self.posture.recipe("gemma4:vlm"))
        self.assertEqual(run.call_args.kwargs["timeout"], 900)
        self.assertIn("same bounded limit", str(failure.exception))
        self.assertNotIn("--bootstrap-recovery", str(failure.exception))


class CancellationProcessTests(FixtureCase):
    """Real process signals at kernel transitions; only model/runtime I/O is fake."""

    @staticmethod
    def run_probe(stage, signum, command):
        # Called only by a disposable subprocess. Never restore dispositions or
        # unblock signals here: this exercises the real command lifetime.
        import ollama_runtime

        case = FixtureCase()
        case.setUp()
        receipt = case.prep.state / "verified.json"
        case.prep.warm(FakeRuntime())
        initial_receipt = receipt.read_bytes()
        print(
            json.dumps(
                {
                    "root": str(case.root),
                    "receipt_sha256": hashlib.sha256(initial_receipt).hexdigest(),
                }
            ),
            flush=True,
        )
        runtime = FakeRuntime()
        runtime.closed = False
        result = {
            "events": [],
            "stops": 0,
            "error": None,
            "fired": False,
            "unwind_steps": [],
            "output_failed": False,
        }
        phase = None
        real_signal = signal.signal
        real_mask = signal.pthread_sigmask
        real_pending = signal.sigpending
        real_complete = module.CommandCancellation.complete
        real_activate = module.CommandCancellation.activate
        real_locked = Preparation.locked
        real_certify = Preparation.certify
        real_invalidate = Preparation.invalidate
        real_verify = runtime.verify

        def fire(event):
            if event == stage and not result["fired"]:
                result["fired"] = True
                os.kill(os.getpid(), signum)

        def install(number, handler):
            event = f"install-{number}"
            result["events"].append(event)
            fire(f"before-{event}")
            if stage == f"fail-{event}":
                result["fired"] = True
                # Setup can fail with both signals already pending, including
                # when neither or only one disposition has been installed.
                os.kill(os.getpid(), signal.SIGTERM)
                os.kill(os.getpid(), signal.SIGINT)
                raise OSError("handler setup failed")
            value = real_signal(number, handler)
            fire(f"after-{event}")
            return value

        def masking(how, numbers):
            transition = None
            if how == signal.SIG_UNBLOCK and len(result["events"]) == 2:
                transition = "operation-unblock"
            if phase == "complete" and how == signal.SIG_BLOCK:
                transition = "complete-block"
            if transition:
                fire(f"before-{transition}")
            value = real_mask(how, numbers)
            if transition:
                fire(f"after-{transition}")
            return value

        def pending():
            if phase == "complete":
                fire("before-pending-observation")
            value = real_pending()
            if phase == "complete":
                fire("after-pending-observation")
            return value

        def activate(controller):
            nonlocal phase
            phase = "activate"
            try:
                return real_activate(controller)
            finally:
                phase = None

        def complete(controller):
            nonlocal phase
            fire("before-complete")
            phase = "complete"
            try:
                value = real_complete(controller)
            finally:
                phase = None
            fire("after-complete")
            return value

        @contextmanager
        def locked(prep):
            with real_locked(prep):
                try:
                    yield
                finally:
                    fire("before-lock-release")
            fire("after-lock-release")

        @contextmanager
        def factory():
            try:
                yield runtime
            finally:
                fire("before-runtime-release")
                runtime.closed = True
                fire("after-runtime-release")

        def certify(prep, *args):
            value = real_certify(prep, *args)
            fire("after-publication")
            return value

        def invalidate(prep):
            if prep._aborting and stage in (
                "repeated-cleanup",
                "stop-failure",
                "broken-diagnostics",
            ):
                result["fired"] = True
                os.kill(os.getpid(), signal.SIGTERM)
                os.kill(os.getpid(), signal.SIGINT)
            value = real_invalidate(prep)
            if prep._aborting and stage == "broken-diagnostics":
                raise OSError("invalidation diagnostic fixture")
            return value

        def stop():
            result["stops"] += 1
            (case.root / "stop-attempted").touch()
            result["runtime_open_at_stop"] = not runtime.closed
            with (case.prep.state / "preparation.lock").open("a+") as contender:
                try:
                    fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    result["lock_held_at_stop"] = True
                else:
                    result["lock_held_at_stop"] = False
            if stage == "stop-failure":
                os.kill(os.getpid(), signal.SIGTERM)
                os.kill(os.getpid(), signal.SIGINT)
                raise OSError("targeted stop fixture failed")
            return {"stopped": True}

        def verify(*args):
            if stage == "swallowed-exception":
                try:
                    raise OSError("internally handled runtime fixture error")
                except OSError:
                    fire(stage)
            if stage == "inflight-work":
                fire(stage)
            result["runtime_returned"] = True
            return real_verify(*args)

        class Output(io.StringIO):
            def write(self, value):
                fire("response-write")
                if stage.startswith("unwind-") or stage in (
                    "broken-output-write",
                    "repeated-cleanup",
                    "stop-failure",
                    "broken-diagnostics",
                    "abort-entry-signal",
                ):
                    result["output_failed"] = True
                    if stage != "abort-entry-signal" and not stage.startswith(
                        "unwind-"
                    ):
                        result["fired"] = True
                    if stage == "unwind-observe":
                        result["fired"] = True
                    raise OSError("response write failed")
                return super().write(value)

            def flush(self):
                fire("response-flush")
                if stage == "broken-output-flush":
                    result["fired"] = True
                    raise OSError("response flush failed")
                return super().flush()

        diagnostics = mock.Mock()
        diagnostics.write.side_effect = OSError("stderr closed")

        def trace(frame, event, _arg):
            if event == "call" and frame.f_code is Preparation.abort.__code__:
                fire("abort-entry-signal")
            if (
                result["output_failed"]
                and result["stops"] == 0
                and event in ("call", "line")
                and frame.f_code
                in (
                    Preparation.adopting.__wrapped__.__code__,
                    Preparation.abort.__code__,
                    Preparation._abort.__code__,
                )
            ):
                result["unwind_steps"].append(
                    f"{frame.f_code.co_name}:{frame.f_lineno}:{event}"
                )
                fire(f"unwind-{len(result['unwind_steps'])}")
            return trace

        argv = [
            "ollama_prepare.py",
            "--config",
            str(case.config),
            "--recipes",
            str(case.recipes),
            "--state",
            str(case.prep.state),
            *command.split(),
        ]
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(module, "Http", return_value=case.api),
            mock.patch.object(Preparation, "create", side_effect=case.api.create),
            mock.patch.object(ollama_runtime, "Runtime", factory),
            mock.patch.object(runtime, "stop_failed", side_effect=stop),
            mock.patch.object(runtime, "verify", side_effect=verify),
            mock.patch.object(Preparation, "locked", locked),
            mock.patch.object(Preparation, "certify", certify),
            mock.patch.object(Preparation, "invalidate", invalidate),
            mock.patch.object(module.CommandCancellation, "activate", activate),
            mock.patch.object(module.CommandCancellation, "complete", complete),
            mock.patch.object(signal, "signal", install),
            mock.patch.object(signal, "pthread_sigmask", masking),
            mock.patch.object(signal, "sigpending", pending),
            mock.patch("sys.stdout", Output()),
            mock.patch("sys.stderr", diagnostics),
        ):
            sys.settrace(trace)
            try:
                module.main()
            except BaseException as exc:
                result["error"] = f"{type(exc).__name__}: {exc}"
            finally:
                sys.settrace(None)
        result["receipt"] = receipt.exists()
        result["unchanged_receipt"] = (
            receipt.exists() and receipt.read_bytes() == initial_receipt
        )
        result["blocked"] = sorted(real_mask(signal.SIG_BLOCK, set()))
        result["pending"] = sorted(real_pending())
        print(json.dumps(result), flush=True)
        case.doCleanups()

    def child(self, stage, signum=signal.SIGTERM, command="warm"):
        script = textwrap.dedent("""\
            import importlib.util, sys
            spec = importlib.util.spec_from_file_location('fixtures', sys.argv[1])
            tests = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(tests)
            tests.CancellationProcessTests.run_probe(sys.argv[2], int(sys.argv[3]), sys.argv[4])
        """)
        return subprocess.run(
            [sys.executable, "-c", script, __file__, stage, str(signum), command],
            env={**os.environ, "TMPDIR": str(self.root)},
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )

    def probe(self, stage, signum=signal.SIGTERM, command="warm"):
        child = self.child(stage, signum, command)
        self.assertEqual(child.returncode, 0, (child.stdout, child.stderr))
        result = json.loads(child.stdout.splitlines()[-1])
        self.assertTrue(result["fired"], (stage, result))
        # Exactly the two handler installations, with no restoration at exit.
        installs = [f"install-{signal.SIGTERM}", f"install-{signal.SIGINT}"]
        if stage == f"fail-install-{signal.SIGTERM}":
            installs.pop()
        self.assertEqual(result["events"], installs)
        self.assertTrue({signal.SIGTERM, signal.SIGINT} <= set(result["blocked"]))
        return result

    def test_plain_verify_real_signals_do_not_mutate_acceptance_or_stop(self):
        terminated = self.child("response-flush", signal.SIGTERM, "verify")
        self.assertEqual(terminated.returncode, -signal.SIGTERM, terminated.stderr)
        initial = json.loads(terminated.stdout.splitlines()[0])
        root = Path(initial["root"])
        receipt = (root / "state" / "verified.json").read_bytes()
        self.assertEqual(hashlib.sha256(receipt).hexdigest(), initial["receipt_sha256"])
        self.assertFalse((root / "stop-attempted").exists())
        interrupted = self.child("response-flush", signal.SIGINT, "verify")
        self.assertEqual(interrupted.returncode, 0, interrupted.stderr)
        result = json.loads(interrupted.stdout.splitlines()[-1])
        self.assertTrue(result["unchanged_receipt"], result)
        self.assertEqual(result["stops"], 0, result)
        self.assertEqual(result["events"], [], result)

    def assert_aborted(self, result):
        self.assertFalse(result["receipt"], result)
        self.assertEqual(result["stops"], 1, result)
        self.assertTrue(result["runtime_open_at_stop"], result)
        self.assertTrue(result["lock_held_at_stop"], result)
        self.assertIn("captured daemon stopped", result["error"])

    def test_signals_before_and_after_each_handler_installation_abort(self):
        for changed in (signal.SIGTERM, signal.SIGINT):
            for side in ("before", "after"):
                for signum in (signal.SIGTERM, signal.SIGINT):
                    with self.subTest(changed=changed, side=side, signal=signum):
                        self.assert_aborted(
                            self.probe(f"{side}-install-{changed}", signum)
                        )

    def test_handler_setup_failure_still_invalidates_and_stops(self):
        for changed in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(changed=changed):
                result = self.probe(f"fail-install-{changed}")
                self.assert_aborted(result)
                self.assertTrue(
                    {signal.SIGTERM, signal.SIGINT} <= set(result["pending"])
                )

    def test_signal_entering_failure_cleanup_cannot_unwind_required_stop(self):
        self.assert_aborted(self.probe("abort-entry-signal"))

    def test_cancellation_recorded_during_swallowed_exception_rejects_completion(self):
        for signum in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=signum):
                result = self.probe("swallowed-exception", signum)
                self.assertTrue(result["runtime_returned"])
                self.assert_aborted(result)

    def test_normal_running_work_is_still_immediately_interruptible(self):
        for signum in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=signum):
                result = self.probe("inflight-work", signum)
                self.assertNotIn("runtime_returned", result)
                self.assert_aborted(result)

    def test_every_pre_stop_failure_unwind_transition_preserves_cleanup(self):
        baseline = self.probe("unwind-observe")
        self.assert_aborted(baseline)
        self.assertGreater(len(baseline["unwind_steps"]), 10)
        for index, location in enumerate(baseline["unwind_steps"], 1):
            for signum in (signal.SIGTERM, signal.SIGINT):
                with self.subTest(location=location, signal=signum):
                    self.assert_aborted(self.probe(f"unwind-{index}", signum))

    def test_precompletion_signals_abort_with_runtime_and_lock_held(self):
        stages = (
            "before-operation-unblock",
            "after-operation-unblock",
            "after-publication",
            "response-write",
            "response-flush",
            "before-complete",
            "before-complete-block",
            "after-complete-block",
            "before-pending-observation",
        )
        for command in ("warm", "verify --stop-on-failure"):
            for stage in stages:
                if command.startswith("verify") and stage == "after-publication":
                    continue
                for signum in (signal.SIGTERM, signal.SIGINT):
                    with self.subTest(command=command, stage=stage, signal=signum):
                        self.assert_aborted(self.probe(stage, signum, command))

    def test_postcompletion_signals_stay_blocked_through_resource_release(self):
        for stage in (
            "after-pending-observation",
            "after-complete",
            "before-runtime-release",
            "after-runtime-release",
            "before-lock-release",
            "after-lock-release",
        ):
            for signum in (signal.SIGTERM, signal.SIGINT):
                with self.subTest(stage=stage, signal=signum):
                    result = self.probe(stage, signum)
                    self.assertTrue(result["receipt"], result)
                    self.assertEqual(result["stops"], 0, result)
                    self.assertIsNone(result["error"], result)
                    self.assertIn(signum, result["pending"])

    def test_output_errors_and_repeated_cleanup_signals_cannot_skip_stop(self):
        for stage in (
            "broken-output-write",
            "broken-output-flush",
            "repeated-cleanup",
            "broken-diagnostics",
        ):
            with self.subTest(stage=stage):
                self.assert_aborted(self.probe(stage))
        result = self.probe("stop-failure")
        self.assertFalse(result["receipt"], result)
        self.assertEqual(result["stops"], 1, result)
        self.assertTrue(result["runtime_open_at_stop"], result)
        self.assertTrue(result["lock_held_at_stop"], result)
        self.assertIn("STOP FAILED: targeted stop fixture failed", result["error"])


class TransportTests(unittest.TestCase):
    def test_only_connection_failures_are_retryable(self):
        errors = [
            urllib.error.URLError(
                ConnectionRefusedError(errno.ECONNREFUSED, "refused")
            ),
            urllib.error.URLError(ConnectionResetError(errno.ECONNRESET, "reset")),
            TimeoutError("timed out"),
        ]
        for error in errors:
            with (
                self.subTest(error=error),
                mock.patch("ollama_recovery.urllib.request.build_opener") as factory,
            ):
                factory.return_value.open.side_effect = error
                with self.assertRaises(TransportUnavailable):
                    Http("http://fixture.invalid").call("/api/version", timeout=0.25)
                self.assertEqual(
                    factory.return_value.open.call_args.kwargs["timeout"], 0.25
                )

    def test_http_rejection_bad_url_permission_and_protocol_are_not_retryable(self):
        errors = [
            urllib.error.HTTPError(
                "http://fixture.invalid", 503, "unavailable", {}, None
            ),
            urllib.error.URLError("unknown URL type"),
            PermissionError(errno.EACCES, "denied"),
        ]
        for error in errors:
            with (
                self.subTest(error=error),
                mock.patch("ollama_recovery.urllib.request.build_opener") as factory,
            ):
                factory.return_value.open.side_effect = error
                with self.assertRaises(PreparationError) as caught:
                    Http("http://fixture.invalid").call("/api/version")
                self.assertNotIsInstance(caught.exception, TransportUnavailable)
        for body in (b"not json", b"[]", b'{"error":"bad request"}'):
            with (
                self.subTest(body=body),
                mock.patch("ollama_recovery.urllib.request.build_opener") as factory,
            ):
                factory.return_value.open.return_value = io.BytesIO(body)
                with self.assertRaises(PreparationError) as caught:
                    Http("http://fixture.invalid").call("/api/version")
                self.assertNotIsInstance(caught.exception, TransportUnavailable)


class RecoveryTests(FixtureCase):
    def recovery_fixture(self):
        models = self.prep.prepare()
        config = {
            "revision": self.posture.revision,
            "version": self.data["version"],
            "residents": self.posture.residents,
        }
        return config, {"schema": 2, **config, "models": models}

    def test_verified_idle_recovery_readback(self):
        config, receipt = self.recovery_fixture()
        self.assertEqual(recover(config, self.api, receipt)["status"], "recovered")

    def test_busy_courtesy_never_loads_or_evicts(self):
        config, receipt = self.recovery_fixture()
        self.api.events.clear()
        self.api.running = [{"name": "operator:model"}]
        result = recover(config, self.api, receipt)
        self.assertEqual(result["status"], "busy")
        self.assertEqual(self.api.events, [])

    def test_missing_or_stale_receipt_refuses_load_even_with_public_alias(self):
        config, receipt = self.recovery_fixture()
        model = self.posture.resident
        witness = f"homelab-prepared/{model.replace(':', '-')}:{self.posture.revision}"
        self.api.models[witness] = copy.deepcopy(self.api.models[model])
        with self.assertRaisesRegex(PreparationError, "verification"):
            recover(config, self.api, None)
        self.mutate_recipe("gemma4:vlm", lambda shown: shown.update(template="new"))
        self.prep.prepare()
        self.api.models[witness] = copy.deepcopy(self.api.models[model])
        with self.assertRaisesRegex(PreparationError, "verification"):
            recover(config, self.api, receipt)

    def test_copresident_recovery_checks_both_models(self):
        self.make_preparation("qwen2.5-coder:fim")
        config, receipt = self.recovery_fixture()
        self.api.evict = True
        with self.assertRaisesRegex(PreparationError, "loaded models"):
            recover(config, self.api, receipt)

    def test_http_load_requires_completed_load_only_response(self):
        api = Http("http://fixture.invalid")
        for response in (
            {},
            {"done": True},
            {"done": False, "done_reason": "load"},
            {"done": True, "done_reason": "stop"},
        ):
            with mock.patch.object(api, "call", return_value=response):
                with self.assertRaisesRegex(PreparationError, "load-only"):
                    api.load("gemma4:vlm")


if __name__ == "__main__":
    unittest.main()
