#!/usr/bin/env python3
"""Preparation/recovery contract tests; fake Ollama, no model or systemd calls."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "_scripts" / "lib"))
from git_env import scrub_environ  # noqa: E402

scrub_environ()

import copy  # noqa: E402
import hashlib  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import tempfile  # noqa: E402
import unittest  # noqa: E402
from unittest import mock  # noqa: E402

HOST = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOST))
import ollama_prepare as module  # noqa: E402
from ollama_prepare import Posture, Preparation  # noqa: E402
from ollama_recovery import Http, PreparationError, recover  # noqa: E402


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

    def call(self, path, body=None, method=None):
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

    def test_files_only_checks_manifest_and_recovery_revision(self):
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
        with self.assertRaisesRegex(PreparationError, "effective recipe"):
            self.prep.files_check(old)
        self.assert_no_candidates()

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
