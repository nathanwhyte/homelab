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
import hashlib  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import tempfile  # noqa: E402
import unittest  # noqa: E402
import urllib.error  # noqa: E402
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

    def test_warm_publishes_witnesses_only_after_runtime_proof(self):
        runtime = FakeRuntime()
        receipt = self.prep.warm(runtime)
        self.assertEqual(runtime.stops, 0)
        self.assertEqual(receipt["revision"], self.posture.revision)
        for tag in self.posture.required:
            self.assertEqual(
                self.api.tags()[self.posture.witness(tag)], self.api.tags()[tag]
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
            if "deepseek" in recipe.name:
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
    def test_verified_idle_recovery_readback(self):
        self.prep.warm(FakeRuntime())
        self.api.running = []
        self.assertEqual(
            recover(self.posture.recovery(), self.api)["status"], "recovered"
        )

    def test_busy_courtesy_never_loads_or_evicts(self):
        self.api.running = [{"name": "operator:model"}]
        result = recover(self.posture.recovery(), self.api)
        self.assertEqual(result["status"], "busy")
        self.assertEqual(self.api.events, [])

    def test_missing_or_stale_witness_refuses_load(self):
        self.prep.prepare()
        with self.assertRaisesRegex(PreparationError, "verification"):
            recover(self.posture.recovery(), self.api)
        self.prep.warm(FakeRuntime())
        stale = self.posture.recovery()
        self.api.running = []
        self.mutate_recipe("gemma4:vlm", lambda shown: shown.update(template="new"))
        self.prep.prepare()
        with self.assertRaisesRegex(PreparationError, "verification"):
            recover(stale, self.api)

    def test_copresident_recovery_checks_both_models(self):
        self.make_preparation("qwen2.5-coder:fim")
        self.prep.warm(FakeRuntime())
        self.api.running = []
        self.api.evict = True
        with self.assertRaisesRegex(PreparationError, "loaded models"):
            recover(self.posture.recovery(), self.api)

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
