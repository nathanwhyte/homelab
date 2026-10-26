"""Hermetic Linux runtime evidence fixtures; no systemctl, signals or models.

Fixtures are constructed from pinned upstream formats, not a live-host capture:
Ollama v0.40.11 llm/llama_server.go:385-424,457 (argv and slog marker);
llama.cpp b11845 server-context.cpp:5550-5565 (/props), server.cpp:547-548
and server-http.cpp:523-524 (listen marker), src/llama.cpp:303-309 (device).
The Vulkan description is verbatim deviceName (ggml-vulkan.cpp:13005-13013).
Mesa 26.2.2 radv_physical_device.c builds marketing_name with a RADV family
annotation and optional nested LLVM version. Journal IDs/timestamps are fake.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "_scripts" / "lib"))
from git_env import scrub_environ  # noqa: E402

scrub_environ()

import json
import signal
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ollama_runtime import Runtime, VerificationError  # noqa: E402


class Host:
    def __init__(self, root):
        self.root = Path(root)
        self.calls = []
        self.closed = []
        self.signals = []
        self.exited = False
        self.exit_on_signal = True
        self.signal_error = None
        self.on_http = None
        self.boot = "11111111-2222-3333-4444-555555555555"
        self.invocation = "a" * 32
        self.props = {
            "MainPID": "100",
            "InvocationID": self.invocation,
            "ControlGroup": "/system.slice/ollama.service",
            "User": "ollama",
            "KillMode": "control-group",
            "RestartPreventExitStatus": "HUP",
            "RestartForceExitStatus": "",
            "SendSIGKILL": "yes",
            "TimeoutStopUSec": "30s",
            "MemoryMax": "17179869184",
        }
        self.env = {
            "OLLAMA_MODELS": "/usr/share/ollama/.ollama/models",
            "OLLAMA_NUM_PARALLEL": "4",
            "OLLAMA_MAX_LOADED_MODELS": "1",
            "OLLAMA_KV_CACHE_TYPE": "q8_0",
            "OLLAMA_FLASH_ATTENTION": "1",
            "OLLAMA_VULKAN": "1",
            "OLLAMA_KEEP_ALIVE": "-1",
        }
        self.expected = {
            "environment": dict(self.env),
            "forbidden_environment": ["GGML_VK_VISIBLE_DEVICES"],
            "parallel": 4,
            "max_loaded": 1,
            "backend": "Vulkan",
            "gpu_name": "AMD Radeon RX 9070 XT",
            "service_properties": {"MemoryMax": "17179869184", "User": "ollama"},
        }
        self.model = {
            "tag": "gemma4:vlm",
            "digest": "sha256:" + "c" * 64,
            "context": 65536,
            "batch": 2048,
            "weights_sha256": "b" * 64,
        }
        self.model_path = self.env["OLLAMA_MODELS"] + "/blobs/sha256-" + "b" * 64
        self.argv = [
            "/usr/lib/ollama/llama-server",
            "--model",
            self.model_path,
            "--port",
            "43210",
            "--host",
            "127.0.0.1",
            "--no-webui",
            "--offline",
            "-c",
            "262144",
            "-np",
            "4",
            "--log-verbosity",
            "4",
            "--no-log-prefix",
            "--no-log-timestamps",
            "--cache-type-k",
            "q8_0",
            "--cache-type-v",
            "q8_0",
            "--flash-attn",
            "on",
            "-b",
            "2048",
            "-ub",
            "2048",
        ]
        self.write("sys/kernel/random/boot_id", self.boot)
        self.process(100, 1, 1000, ["/usr/local/bin/ollama", "serve"])
        self.process(101, 100, 2000, self.argv)
        self.write(
            "100/environ", "\0".join(f"{k}={v}" for k, v in self.env.items()) + "\0"
        )
        self.http_data = {
            "model_path": self.model_path,
            "model_alias": self.model_path,
            "modalities": {"vision": True, "video": False, "audio": False},
            "total_slots": 4,
            "default_generation_settings": {"params": {}, "n_ctx": 65536},
        }
        self.messages = [
            'time=now level=INFO source=llama_server.go:457 msg="starting llama-server" cmd='
            + json.dumps(" ".join(self.argv)),
            "llama_model_load_from_file_impl: using device Vulkan0 (AMD Radeon RX 9070 XT (RADV GFX1201)) (0000:03:00.0) - 16000 MiB free",
            f"llama_model_loader: loaded meta data with 42 key-value pairs and 400 tensors from {self.model_path} (version GGUF V3 (latest))",
            "load_tensors: offloaded 43/43 layers to GPU",
            "llama_context: n_ctx                 = 262144",
            "llama_context: n_ctx_seq             = 65536",
            "llama_context: n_batch               = 2048",
            "llama_context: n_ubatch              = 2048",
            "llama_context: flash_attn            = enabled",
            "llama_kv_cache: size = 2176.00 MiB (262144 cells, 36 layers, 4/4 seqs), K (q8_0): 1088.00 MiB, V (q8_0): 1088.00 MiB",
            "srv initialize: initializing, n_slots = 4, n_ctx_slot = 65536, kv_unified = 'false'",
            "srv main: model loaded",
            "srv main: listening on http://127.0.0.1:43210",
        ]
        self.rows_override = None

    def write(self, path, value):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(value if isinstance(value, bytes) else value.encode())

    def process(self, pid, ppid, start, argv):
        fields = ["S", str(ppid)] + ["0"] * 17 + [str(start)] + ["0"] * 5
        self.write(f"{pid}/stat", f"{pid} (process with ) parens) " + " ".join(fields))
        self.write(f"{pid}/cmdline", "\0".join(argv) + "\0")
        self.write(
            f"{pid}/status",
            "Uid:\t992\t992\t992\t992\nSigIgn:\t0000\nSigBlk:\t0000\nSigCgt:\t0001\n",
        )
        self.write(f"{pid}/cgroup", "0::/system.slice/ollama.service\n")
        exe = self.root / str(pid) / "exe"
        if exe.is_symlink():
            exe.unlink()
        exe.symlink_to(argv[0])

    def rows(self):
        return [
            {
                "_BOOT_ID": self.boot.replace("-", ""),
                "_SYSTEMD_INVOCATION_ID": self.invocation,
                "_PID": "100",
                "__MONOTONIC_TIMESTAMP": str(19_995_000 + i * 10000),
                "MESSAGE": message,
            }
            for i, message in enumerate(self.messages)
        ]

    def command(self, argv):
        self.calls.append(argv)
        if argv[0] == "systemctl":
            names = (
                next(a for a in argv if a.startswith("--property="))
                .split("=", 1)[1]
                .split(",")
            )
            return "\n".join(f"{k}={self.props[k]}" for k in names)
        if argv[0] == "journalctl":
            return "\n".join(
                json.dumps(row)
                for row in (
                    self.rows_override
                    if self.rows_override is not None
                    else self.rows()
                )
            )
        raise AssertionError(f"unexpected command {argv}")

    def http(self, port):
        assert port == 43210
        if self.on_http:
            self.on_http()
        return self.http_data

    def send(self, fd, signum, info, flags):
        self.signals.append((fd, signum, info, flags))
        if self.signal_error:
            raise self.signal_error
        self.exited = self.exit_on_signal

    def runtime(self):
        return Runtime(
            proc_root=self.root,
            run=self.command,
            http_get=self.http,
            pidfd_open=lambda pid, flags: 900 + pid,
            pidfd_send_signal=self.send,
            wait_exit=lambda fd, timeout: self.exited,
            close_fd=self.closed.append,
            user_uid=lambda name: 992,
            clock_ticks=100,
        )


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.host = Host(self.temp.name)
        self.runtime = self.host.runtime()
        self.addCleanup(self.runtime.close)

    def verify(self):
        return self.runtime.verify(self.host.expected, [self.host.model])

    def test_captured_daemon_and_model_evidence(self):
        result = self.verify()
        self.assertEqual(result["pid"], 100)
        self.assertEqual(result["models"][0]["gpu_layers"], 43)
        self.assertEqual(result["models"][0]["slots"], 4)
        self.assertEqual(result["models"][0]["n_ctx_seq"], 65536)
        self.assertNotIn("environment", result)

    def test_supported_radv_description_annotations(self):
        original = self.host.messages[1]
        base = "AMD Radeon RX 9070 XT"
        for name in [
            base,
            base + " (RADV GFX1201)",
            base + " (RADV GFX1201 (LLVM 9.0.1))",
            base + " (RADV GFX1201 (LLVM 21.1.0git))",
        ]:
            with self.subTest(name=name):
                self.host.messages[1] = original.replace(base + " (RADV GFX1201)", name)
                evidence = self.verify()["models"][0]
                self.assertEqual(evidence["gpu_name"], name)
                self.assertEqual(evidence["pci_id"], "0000:03:00.0")

    def test_gpu_name_substrings_and_unknown_suffixes_are_rejected(self):
        original = self.host.messages[1]
        base = "AMD Radeon RX 9070 XT"
        names = [
            "AMD Radeon RX 9070 (RADV GFX1201)",
            "AMD Radeon RX 9060 XT (RADV GFX1200)",
            "AMD Radeon Graphics (RADV RAPHAEL)",
            base + "X (RADV GFX1201)",
            "Other " + base + " (RADV GFX1201)",
            base + " (llvmpipe)",
            base + " (RADV GFX1201) trailing",
            base + " (RADV GFX1201) (extra)",
            base + " (RADV GFX1201 (LLVM unknown))",
            base + " (RADV GFX1201 garbage)",
        ]
        for name in names:
            with self.subTest(name=name):
                self.host.messages[1] = original.replace(base + " (RADV GFX1201)", name)
                with self.assertRaisesRegex(
                    VerificationError, "backend or GPU differs"
                ):
                    self.verify()

    def test_radv_annotation_does_not_expand_other_backend_names(self):
        self.host.expected["backend"] = "ROCm"
        self.host.messages[1] = self.host.messages[1].replace("Vulkan0", "ROCm0")
        with self.assertRaisesRegex(VerificationError, "backend or GPU differs"):
            self.verify()

    def test_multiline_journal_messages_preserve_startup_fields(self):
        self.host.messages[4:10] = ["\n".join(self.host.messages[4:10])]
        self.verify()

    def test_two_sequential_residents_are_bound_independently(self):
        self.host.expected["max_loaded"] = 2
        self.host.expected["environment"]["OLLAMA_MAX_LOADED_MODELS"] = "2"
        self.host.write(
            "100/environ",
            "\0".join(
                f"{key}={value}"
                for key, value in self.host.expected["environment"].items()
            )
            + "\0",
        )
        second = {
            **self.host.model,
            "tag": "qwen2.5-coder:fim",
            "weights_sha256": "e" * 64,
        }
        second_path = self.host.model_path.replace("b" * 64, "e" * 64)
        second_argv = [
            arg.replace(self.host.model_path, second_path).replace("43210", "43211")
            for arg in self.host.argv
        ]
        self.host.process(102, 100, 2013, second_argv)
        self.host.messages.extend(
            [
                message.replace(self.host.model_path, second_path).replace(
                    "43210", "43211"
                )
                for message in self.host.messages[:]
            ]
        )
        self.runtime.http_get = lambda port: {
            **self.host.http_data,
            "model_path": self.host.model_path if port == 43210 else second_path,
        }
        evidence = self.runtime.verify(self.host.expected, [self.host.model, second])
        self.assertEqual([model["pid"] for model in evidence["models"]], [101, 102])

    def test_daemon_environment_mismatch(self):
        self.host.write("100/environ", "OLLAMA_NUM_PARALLEL=8\0")
        with self.assertRaisesRegex(VerificationError, "environment differs"):
            self.verify()

    def test_forbidden_setting_and_duplicate_environment(self):
        original = (self.host.root / "100/environ").read_bytes()
        for added in [b"GGML_VK_VISIBLE_DEVICES=0\0", b"OLLAMA_NUM_PARALLEL=4\0"]:
            with self.subTest(added=added):
                self.host.write("100/environ", original + added)
                with self.assertRaises(VerificationError):
                    self.verify()

    def test_effective_service_property(self):
        self.host.props["MemoryMax"] = "infinity"
        with self.assertRaisesRegex(VerificationError, "MemoryMax"):
            self.verify()

    def test_wrong_owner_or_control_group(self):
        for path, value in [
            ("101/status", "Uid:\t0\t0\t0\t0\n"),
            ("101/cgroup", "0::/other.service\n"),
        ]:
            with self.subTest(path=path):
                original = (self.host.root / path).read_bytes()
                self.host.write(path, value)
                with self.assertRaises(VerificationError):
                    self.verify()
                self.host.write(path, original)

    def test_daemon_replacement_is_not_adopted(self):
        self.host.on_http = lambda: self.host.props.update(InvocationID="d" * 32)
        with self.assertRaisesRegex(VerificationError, "generation changed"):
            self.verify()

    def test_runner_replacement_is_not_adopted(self):
        self.host.on_http = lambda: self.host.process(101, 100, 3000, self.host.argv)
        with self.assertRaisesRegex(VerificationError, "runner generation changed"):
            self.verify()

    def test_extra_runner_is_interference(self):
        self.host.process(102, 100, 2001, self.host.argv)
        with self.assertRaisesRegex(VerificationError, "runner count"):
            self.verify()

    def test_wrong_weights_are_not_same_model(self):
        self.host.model["weights_sha256"] = "e" * 64
        with self.assertRaisesRegex(VerificationError, "expected weights"):
            self.verify()

    def test_changed_and_duplicate_argv(self):
        for suffix in [["-np", "8"], ["--cache-type-k", "f16"], ["--ctx-size=131072"]]:
            with self.subTest(suffix=suffix):
                self.host.write(
                    "101/cmdline", "\0".join(self.host.argv + suffix) + "\0"
                )
                with self.assertRaises(VerificationError):
                    self.verify()

    def test_props_is_actual_readback(self):
        self.host.http_data["default_generation_settings"]["n_ctx"] = 32768
        with self.assertRaisesRegex(VerificationError, "/props differs"):
            self.verify()

    def test_actual_runtime_mismatch(self):
        mutations = {
            "Vulkan0": "ROCm0",
            "AMD Radeon RX 9070 XT": "AMD Radeon Graphics",
            "offloaded 43/43": "offloaded 42/43",
            "= 262144": "= 131072",
            "= 65536": "= 32768",
            "= 2048": "= 512",
            "= enabled": "= auto",
            "K (q8_0)": "K (f16)",
            "V (q8_0)": "V (f16)",
        }
        original = self.host.messages[:]
        for before, after in mutations.items():
            with self.subTest(before=before):
                self.host.messages = [m.replace(before, after) for m in original]
                with self.assertRaises(VerificationError):
                    self.verify()

    def test_missing_and_ambiguous_startup_evidence(self):
        original = self.host.messages[:]
        for index in [0, 1, 2, 3, 4, 7, 8, 9, 12]:
            with self.subTest(index=index):
                self.host.messages = original[:index] + original[index + 1 :]
                with self.assertRaises(VerificationError):
                    self.verify()
        self.host.messages = original[:4] + [original[3]] + original[4:]
        with self.assertRaisesRegex(VerificationError, "ambiguous layer offload"):
            self.verify()

    def test_new_start_before_ready_is_ambiguous(self):
        self.host.messages.insert(4, self.host.messages[0].replace("43210", "43211"))
        with self.assertRaisesRegex(VerificationError, "listening marker"):
            self.verify()

    def test_stale_or_wrong_generation_journal(self):
        for key, value in [
            ("_PID", "99"),
            ("_SYSTEMD_INVOCATION_ID", "e" * 32),
            ("_BOOT_ID", "f" * 32),
            ("__MONOTONIC_TIMESTAMP", "1000"),
        ]:
            with self.subTest(key=key):
                self.host.rows_override = self.host.rows()
                for row in self.host.rows_override:
                    row[key] = value
                with self.assertRaises(VerificationError):
                    self.verify()

    def test_proc_start_rounding_allows_one_clock_tick(self):
        self.host.rows_override = self.host.rows()
        self.host.rows_override[0]["__MONOTONIC_TIMESTAMP"] = "20005000"
        self.verify()

    def test_unsupported_journal_shape_fails_closed(self):
        self.host.rows_override = self.host.rows()
        self.host.rows_override[0]["MESSAGE"] = [255, 0]
        with self.assertRaisesRegex(VerificationError, "journal identity mismatch"):
            self.verify()

    def test_signal_policy_is_required_even_for_success(self):
        for key, value in [
            ("RestartPreventExitStatus", "TERM"),
            ("RestartForceExitStatus", "HUP"),
            ("KillMode", "process"),
            ("SendSIGKILL", "no"),
            ("TimeoutStopUSec", "infinity"),
        ]:
            with self.subTest(key=key):
                old = self.host.props[key]
                self.host.props[key] = value
                with self.assertRaises(VerificationError):
                    self.verify()
                self.host.props[key] = old

    def test_stop_uses_only_captured_pidfd(self):
        result = self.runtime.stop_failed()
        self.assertTrue(result["stopped"])
        self.assertEqual(self.host.signals, [(1000, signal.SIGHUP, None, 0)])
        self.assertFalse(
            any("stop" in call or "kill" in call for call in self.host.calls)
        )

    def test_exit_before_signal_never_targets_replacement(self):
        self.host.exited = True
        self.host.props.update(MainPID="101", InvocationID="b" * 32)
        self.assertTrue(self.runtime.stop_failed()["already_exited"])
        self.assertEqual(self.host.signals, [])

    def test_replacement_after_last_check_keeps_original_pidfd(self):
        def send(fd, signum, info, flags):
            self.host.props.update(MainPID="500", InvocationID="b" * 32)
            self.host.send(fd, signum, info, flags)

        self.runtime.send_signal = send
        self.runtime.stop_failed()
        self.assertEqual(self.host.signals[0][0], 1000)

    def test_pid_reuse_before_capture_finishes_closes_fd(self):
        self.runtime.close()
        calls = 0

        def run(argv):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.host.process(100, 1, 9999, ["/usr/local/bin/ollama", "serve"])
            return self.host.command(argv)

        with self.assertRaisesRegex(VerificationError, "generation changed"):
            Runtime(
                proc_root=self.host.root,
                run=run,
                pidfd_open=lambda *_: 345,
                pidfd_send_signal=self.host.send,
                wait_exit=lambda *_: False,
                close_fd=self.host.closed.append,
                clock_ticks=100,
            )
        self.assertIn(345, self.host.closed)

    def test_esrch_does_not_reacquire_pid(self):
        self.host.signal_error = ProcessLookupError()
        self.assertTrue(self.runtime.stop_failed()["already_exited"])
        self.assertEqual(len(self.host.signals), 1)

    def test_signal_permission_or_timeout_is_failure(self):
        self.host.signal_error = PermissionError()
        with self.assertRaisesRegex(VerificationError, "could not signal"):
            self.runtime.stop_failed()
        self.host.signal_error = None
        self.host.exit_on_signal = False
        with self.assertRaisesRegex(VerificationError, "stop deadline"):
            self.runtime.stop_failed(timeout=0.01)

    def test_ignored_and_blocked_hup_never_signals(self):
        for key in ["SigIgn", "SigBlk"]:
            with self.subTest(key=key):
                self.host.write(
                    "100/status",
                    "Uid:\t992\t992\t992\t992\nSigIgn:\t0\nSigBlk:\t0\n".replace(
                        key + ":\t0", key + ":\t1"
                    ),
                )
                with self.assertRaisesRegex(VerificationError, "ignores or blocks"):
                    self.runtime.stop_failed()
                self.assertEqual(self.host.signals, [])

    def test_closed_runtime_cannot_signal(self):
        self.runtime.close()
        with self.assertRaisesRegex(VerificationError, "no captured"):
            self.runtime.stop_failed()


if __name__ == "__main__":
    unittest.main()
