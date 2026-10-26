#!/usr/bin/env python3
"""Recovery attestation uses real locks/files and fake proc/API/HTTP handlers."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "_scripts" / "lib"))
from git_env import scrub_environ  # noqa: E402

scrub_environ()

import copy  # noqa: E402
from email.message import Message  # noqa: E402
import fcntl  # noqa: E402
import importlib.util  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import tempfile  # noqa: E402
from types import SimpleNamespace  # noqa: E402
import unittest  # noqa: E402
from unittest import mock  # noqa: E402

HOST = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOST))
import ollama_attestation as attestation  # noqa: E402
import ollama_recovery as recovery  # noqa: E402
from ollama_recovery import PreparationError  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "exporter", HOST.parent / "ollama/ollama-exporter.py"
)
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)

BOOT = "01234567-89ab-cdef-0123-456789abcdef"


class FakeApi:
    def __init__(self, models):
        self.models = copy.deepcopy(models)
        self.running = []
        self.events = []
        self.on_load = None
        self.version = "0.40.11"

    def loaded(self):
        self.events.append("ps")
        return copy.deepcopy(self.running)

    def call(self, path):
        self.events.append(path)
        if path != "/api/version":
            raise AssertionError(path)
        return {"version": self.version}

    def tags(self):
        self.events.append("tags")
        return {name: value["digest"] for name, value in self.models.items()}

    def load(self, name):
        self.events.append(("load", name))
        if self.on_load:
            self.on_load()
        model = self.models[name]
        self.running.append(
            {
                "name": name,
                "digest": model["digest"],
                "context_length": model["context"],
                "size_vram": 123,
            }
        )


class AttestationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.state = self.root / "state"
        self.state.mkdir(mode=0o755)
        (self.state / "preparation.lock").touch(mode=0o644)
        self.config = {
            "version": "0.40.11",
            "revision": "a" * 64,
            "residents": ["gemma4:vlm"],
        }
        self.receipt = {
            "schema": 2,
            **self.config,
            "resident": "gemma4:vlm",
            "models": {"gemma4:vlm": {"digest": "b" * 64, "context": 65536}},
            "runtime": {"boot_id": BOOT, "pid": 42, "start_ticks": 123},
        }
        self.save_receipt()
        self.proc = self.root / "proc"
        (self.proc / "sys/kernel/random").mkdir(parents=True)
        (self.proc / "sys/kernel/random/boot_id").write_text(BOOT)
        (self.proc / "42").mkdir()
        self.write_stat(123)
        (self.proc / "42/status").write_text(
            f"Name:\tollama\nUid:\t{os.getuid()}\t{os.getuid()}\t{os.getuid()}\t{os.getuid()}\n"
        )
        (self.proc / "42/cmdline").write_bytes(b"/usr/local/bin/ollama\0serve\0")
        (self.proc / "42/exe").symlink_to("/usr/local/bin/ollama")
        self.api = FakeApi(self.receipt["models"])

    def save_receipt(self):
        path = self.state / "verified.json"
        path.write_text(json.dumps(self.receipt))
        path.chmod(0o644)

    def write_stat(self, ticks):
        fields = ["S"] + ["0"] * 18 + [str(ticks)]
        (self.proc / "42/stat").write_text(
            "42 (ollama with ) character) " + " ".join(fields)
        )

    def run_recovery(self, **overrides):
        args = {
            "owner_uid": os.getuid(),
            "daemon_uid": os.getuid(),
            "proc_root": self.proc,
        }
        args.update(overrides)
        return attestation._recover_attested(self.config, self.state, self.api, **args)

    def assert_lock_free(self):
        with (self.state / "preparation.lock").open("rb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_happy_path_reads_receipt_and_holds_readonly_lock(self):
        before = (self.state / "verified.json").read_bytes()
        opened = []
        original = os.open

        def record(path, flags, *args, **kwargs):
            opened.append((path, flags))
            return original(path, flags, *args, **kwargs)

        def during_load():
            with (self.state / "preparation.lock").open("rb") as other:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)

        self.api.on_load = during_load
        with mock.patch.object(attestation.os, "open", side_effect=record):
            result = self.run_recovery()
        self.assertEqual(result["status"], "recovered")
        self.assertTrue(result["changed"])
        self.assertEqual((self.state / "verified.json").read_bytes(), before)
        for _, flags in opened:
            self.assertFalse(
                flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)
            )
        self.assert_lock_free()

    def test_alias_cannot_authorize_missing_or_partial_receipt(self):
        self.api.models["homelab-prepared/gemma4-vlm:" + "a" * 64] = copy.deepcopy(
            self.receipt["models"]["gemma4:vlm"]
        )
        for content in (None, b"", b'{"schema":2}', b'{"schema":2,'):
            with self.subTest(content=content):
                path = self.state / "verified.json"
                path.unlink(missing_ok=True)
                if content is not None:
                    path.write_bytes(content)
                with self.assertRaises(PreparationError):
                    self.run_recovery()
                self.assertEqual(self.api.events, [])

    def test_wrong_revision_version_retired_profile_and_legacy_schema_refuse(self):
        changes = [
            {"revision": "c" * 64},
            {"version": "0.40.10"},
            {"residents": ["qwen2.5-coder:fim"]},
            {"schema": 1},
            {"models": {}},
            {"models": {"gemma4:vlm": {"digest": "b" * 64, "context": True}}},
        ]
        original = copy.deepcopy(self.receipt)
        for change in changes:
            with self.subTest(change=change):
                self.receipt = {**original, **change}
                self.save_receipt()
                with self.assertRaises(PreparationError):
                    self.run_recovery()
                self.assertEqual(self.api.events, [])

    def test_forged_alias_and_serving_tag_cannot_replace_receipted_digest(self):
        self.api.models["gemma4:vlm"]["digest"] = "c" * 64
        self.api.models["homelab-prepared/gemma4-vlm:" + "a" * 64] = copy.deepcopy(
            self.api.models["gemma4:vlm"]
        )
        with self.assertRaisesRegex(PreparationError, "stale host verification"):
            self.run_recovery()
        self.assertFalse(any(isinstance(event, tuple) for event in self.api.events))

    def test_busy_courtesy_does_not_load_or_query_models(self):
        self.api.running = [{"name": "operator:model"}]
        self.assertEqual(self.run_recovery(), {"status": "busy", "changed": False})
        self.assertEqual(self.api.events, ["ps"])

    def test_preparation_lock_contention_is_noop(self):
        with (self.state / "preparation.lock").open("rb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.run_recovery()
        self.assertEqual(result["reason"], "preparation-active")
        self.assertFalse(result["changed"])
        self.assertEqual(self.api.events, [])

    def test_wrong_boot_start_time_uid_and_command_refuse_before_api(self):
        changes = [
            ("sys/kernel/random/boot_id", b"ffffffff-ffff-ffff-ffff-ffffffffffff"),
            ("42/stat", b"42 (ollama) S " + b"0 " * 18 + b"124"),
            ("42/status", b"Uid:\t9999\t9999\t9999\t9999\n"),
            ("42/cmdline", b"/usr/local/bin/ollama\0run\0"),
        ]
        for relative, changed in changes:
            with self.subTest(path=relative):
                path = self.proc / relative
                original = path.read_bytes()
                path.write_bytes(changed)
                with self.assertRaises(PreparationError):
                    self.run_recovery()
                path.write_bytes(original)
                self.assertEqual(self.api.events, [])

    def test_process_exit_and_replacement_during_load_never_report_success(self):
        self.api.on_load = lambda: self.write_stat(124)
        with self.assertRaisesRegex(PreparationError, "generation"):
            self.run_recovery()
        self.assert_lock_free()

    def test_receipt_replace_or_inplace_change_during_load_never_report_success(self):
        def replace():
            other = self.state / "new.json"
            other.write_bytes((self.state / "verified.json").read_bytes())
            other.replace(self.state / "verified.json")

        def modify():
            self.receipt["revision"] = "d" * 64
            self.save_receipt()

        for change in (replace, modify):
            with self.subTest(change=change.__name__):
                self.api.running = []
                self.api.on_load = change
                with self.assertRaisesRegex(
                    PreparationError, "changed during recovery"
                ):
                    self.run_recovery()
                self.assert_lock_free()

    def test_wrong_owner_or_writable_state_refuses(self):
        with self.assertRaisesRegex(PreparationError, "root-owned"):
            self.run_recovery(owner_uid=os.getuid() + 1)
        for path in (
            self.state,
            self.state / "preparation.lock",
            self.state / "verified.json",
        ):
            with self.subTest(path=path.name):
                original = path.stat().st_mode & 0o777
                path.chmod(original | 0o020)
                with self.assertRaisesRegex(PreparationError, "root-owned"):
                    self.run_recovery()
                path.chmod(original)
        self.assertEqual(self.api.events, [])

    def test_symlinked_files_and_state_directory_refuse(self):
        for name in ("preparation.lock", "verified.json"):
            with self.subTest(name=name):
                path = self.state / name
                moved = self.root / name
                path.rename(moved)
                path.symlink_to(moved)
                with self.assertRaises(PreparationError):
                    self.run_recovery()
                path.unlink()
                moved.rename(path)
        alias = self.root / "state-alias"
        alias.symlink_to(self.state, target_is_directory=True)
        self.state = alias
        with self.assertRaises(PreparationError):
            self.run_recovery()
        self.assertEqual(self.api.events, [])

    def test_failed_load_and_cancellation_release_lock_without_writing_receipt(self):
        before = (self.state / "verified.json").read_bytes()
        for error in (PreparationError("load failed"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                self.api.on_load = mock.Mock(side_effect=error)
                with self.assertRaises(type(error)):
                    self.run_recovery()
                self.assert_lock_free()
                self.assertEqual((self.state / "verified.json").read_bytes(), before)

    def test_production_entry_cannot_override_ownership_or_endpoint(self):
        with (
            mock.patch.object(
                attestation.pwd, "getpwnam", return_value=SimpleNamespace(pw_uid=123)
            ),
            mock.patch.object(
                attestation,
                "_recover_attested",
                return_value={"status": "busy", "changed": False},
            ) as run,
        ):
            attestation.recover_attested(self.config, self.state)
        args, kwargs = run.call_args
        self.assertEqual(args[2].url, "http://127.0.0.1:11434")
        self.assertEqual(
            kwargs, {"owner_uid": 0, "daemon_uid": 123, "proc_root": "/proc"}
        )


class ExporterTests(unittest.TestCase):
    def handler(self, body=b"{}", adapter=None, path="/preparation/recover"):
        handler = object.__new__(exporter.MetricsHandler)
        handler.path = path
        handler.headers = Message()
        handler.headers["Content-Length"] = str(len(body))
        handler.connection = mock.Mock()
        handler.rfile, handler.wfile = io.BytesIO(body), io.BytesIO()
        handler.server = SimpleNamespace(preparation_recover=adapter)
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        return handler

    def test_recovery_route_disabled_by_default_and_optional_import_unused(self):
        with mock.patch(
            "builtins.__import__",
            side_effect=AssertionError("unexpected optional import"),
        ):
            self.assertIsNone(exporter.preparation_adapter(None))
        handler = self.handler()
        handler.do_POST()
        handler.send_response.assert_called_once_with(404)

    def test_recovery_route_validates_boundaries_before_adapter(self):
        adapter = mock.Mock()
        for raw in (
            b"[]",
            b"x",
            b"{}",
            b"x" * 4097,
            b'{"revision":"a","version":"b","residents":[],"state":"/tmp/forged"}',
        ):
            with self.subTest(raw=raw[:60]):
                handler = self.handler(raw, adapter)
                handler.do_POST()
                handler.send_response.assert_called_once_with(400)
        adapter.assert_not_called()
        for header, value in (
            ("Content-Length", "2"),
            ("Transfer-Encoding", "chunked"),
        ):
            handler = self.handler(b"{}", adapter)
            handler.headers[header] = value
            handler.do_POST()
            handler.send_response.assert_called_once_with(400)
        adapter.assert_not_called()

    def test_recovery_route_returns_completed_adapter_result_or_bounded_error(self):
        config = {
            "version": "0.40.11",
            "revision": "a" * 64,
            "residents": ["gemma4:vlm"],
        }
        adapter = mock.Mock(return_value={"status": "recovered", "changed": True})
        handler = self.handler(json.dumps(config).encode(), adapter)
        handler.do_POST()
        adapter.assert_called_once_with(config)
        handler.send_response.assert_called_once_with(200)
        self.assertEqual(json.loads(handler.wfile.getvalue()), adapter.return_value)
        handler = self.handler(
            json.dumps(config).encode(),
            mock.Mock(side_effect=PreparationError("bad" * 1000)),
        )
        handler.do_POST()
        handler.send_response.assert_called_once_with(409)
        self.assertLessEqual(len(json.loads(handler.wfile.getvalue())["error"]), 300)

    def test_metrics_and_push_routes_remain_available_without_adapter(self):
        handler = self.handler(b'{"model":"test"}', path="/push")
        handler.do_POST()
        handler.send_response.assert_called_once_with(204)
        handler = self.handler(path="/metrics")
        handler.do_GET()
        handler.send_response.assert_called_once_with(200)
        self.assertIn(b"ollama_request_total", handler.wfile.getvalue())

    def test_standalone_client_calls_only_recovery_endpoint(self):
        config = {
            "version": "0.40.11",
            "revision": "a" * 64,
            "residents": ["gemma4:vlm"],
        }
        with mock.patch.object(
            recovery.Http, "call", return_value={"status": "busy", "changed": False}
        ) as call:
            recovery.request_recovery(config)
        call.assert_called_once_with("/preparation/recover", config, timeout=660)
        with mock.patch.object(recovery.Http, "call", return_value={}) as call:
            with self.assertRaisesRegex(PreparationError, "completed outcome"):
                recovery.request_recovery(config)
        with self.assertRaisesRegex(PreparationError, "endpoint"):
            recovery.request_recovery(config, "http://fixture.invalid/api/generate")

    def test_http_refusal_preserves_bounded_adapter_diagnostic(self):
        reason = "receipt revision differs; adopt the declared posture\n\x1b[31m"
        body = io.BytesIO(json.dumps({"error": reason}).encode())
        error = recovery.urllib.error.HTTPError(
            "http://fixture.invalid", 409, "Conflict", {}, body
        )
        with mock.patch.object(recovery.urllib.request, "build_opener") as opener:
            opener.return_value.open.side_effect = error
            with self.assertRaises(PreparationError) as raised:
                recovery.Http("http://fixture.invalid").call("/preparation/recover", {})
        self.assertIn("HTTP 409", str(raised.exception))
        self.assertIn("receipt revision differs", str(raised.exception))
        self.assertNotIn("\n", str(raised.exception))
        self.assertNotIn("\x1b", str(raised.exception))
        self.assertTrue(body.closed)

    def test_unusable_http_diagnostic_preserves_status_and_closes_response(self):
        for raw in (b"not JSON", b"[]", b'{"error":null}', b"x" * 5000):
            with self.subTest(raw=raw[:30]):
                body = io.BytesIO(raw)
                error = recovery.urllib.error.HTTPError(
                    "http://fixture.invalid", 409, "Conflict", {}, body
                )
                with mock.patch.object(
                    recovery.urllib.request, "build_opener"
                ) as opener:
                    opener.return_value.open.side_effect = error
                    with self.assertRaisesRegex(
                        PreparationError, "^/preparation/recover: HTTP 409$"
                    ):
                        recovery.Http("http://fixture.invalid").call(
                            "/preparation/recover", {}
                        )
                self.assertTrue(body.closed)

    def test_http_diagnostic_read_failure_preserves_status(self):
        body = io.BytesIO()
        body.read = mock.Mock(side_effect=OSError("response interrupted"))
        error = recovery.urllib.error.HTTPError(
            "http://fixture.invalid", 409, "Conflict", {}, body
        )
        with mock.patch.object(recovery.urllib.request, "build_opener") as opener:
            opener.return_value.open.side_effect = error
            with self.assertRaisesRegex(PreparationError, "HTTP 409"):
                recovery.Http("http://fixture.invalid").call("/preparation/recover", {})
        body.read.assert_called_once_with(4097)
        self.assertTrue(body.closed)


if __name__ == "__main__":
    unittest.main()
