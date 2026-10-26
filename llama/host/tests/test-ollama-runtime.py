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

from contextlib import contextmanager
import errno
import json
import signal
import struct
import subprocess
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ollama_runtime import Runtime, VerificationError, _task_start  # noqa: E402


class TaskstatsSocket:
    """Byte fixtures from Linux v6.17 taskstats.h/netlink.h/genetlink.h."""

    def __init__(self, *, pid=101, elapsed=10_000_000):
        self.sent = []
        self.closed = False
        self.flags = 0
        self.sender = (0, 0)
        self.error = None
        # Fixed UAPI prefix, including its explicit 8-byte field alignment.
        stats = struct.pack(
            "=H2xIBB6x8Q32sB7x5I4xQ",
            16,
            0,
            0,
            0,
            *([0] * 8),
            b"llama-server",
            0,
            992,
            992,
            pid,
            100,
            0,
            elapsed,
        )
        self.replies = [
            self.packet(16, 1, 1, self.attr(1, struct.pack("=H", 40))),
            self.packet(
                40,
                2,
                2,
                self.attr(
                    4, self.attr(1, struct.pack("=I", pid)) + self.attr(3, stats)
                ),
            ),
        ]

    @staticmethod
    def attr(kind, data):
        length = len(data) + 4
        return struct.pack("=HH", length, kind) + data + bytes(-length % 4)

    @staticmethod
    def packet(family, command, sequence, attrs):
        body = struct.pack("=BBH", command, 1, 0) + attrs
        return struct.pack("=IHHII", 16 + len(body), family, 0, sequence, 1234) + body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        self.closed = True

    def settimeout(self, value):
        self.timeout = value

    def bind(self, address):
        assert address == (0, 0)

    def sendto(self, message, address):
        assert address == (0, 0)
        self.sent.append(message)

    def recvmsg(self, size):
        assert size == 65536
        if self.error:
            raise self.error
        return self.replies.pop(0), [], self.flags, self.sender


class TaskstatsTests(unittest.TestCase):
    def read(self, sock, times=(30_000_000_000, 30_000_100_000)):
        clock = iter(times)
        return _task_start(
            101, socket_factory=lambda *_: sock, monotonic_ns=lambda: next(clock)
        )

    def test_monotonic_birth_bounds_and_exact_read_only_request(self):
        sock = TaskstatsSocket()
        self.assertEqual(self.read(sock), (19_999_999, 20_000_100))
        # GET FAMILY, then TASKSTATS GET for this PID. Never register a listener.
        self.assertEqual(sock.sent[0][16:20], bytes([3, 1, 0, 0]))
        self.assertEqual(sock.sent[1][16:], bytes.fromhex("010100000800010065000000"))
        self.assertEqual(sock.timeout, 5)
        self.assertTrue(sock.closed)

    def test_suspend_during_request_does_not_shift_monotonic_birth(self):
        # A 60-second suspend advances BOOTTIME only. The kernel computes
        # ac_etime in the same monotonic clock as both userspace samples.
        sock = TaskstatsSocket(elapsed=10_000_050)
        self.assertEqual(self.read(sock), (19_999_949, 20_000_050))

    def test_timeout_and_permission_failure_have_recovery_guidance(self):
        for error, diagnostic in [
            (TimeoutError(), "request timed out"),
            (PermissionError(errno.EPERM, "denied"), "CONFIG_TASKSTATS.*CAP_NET_ADMIN"),
            (OSError(errno.EIO, "transport"), "transport failed"),
            (ValueError("decode"), "malformed TASKSTATS protocol"),
            (struct.error("decode"), "malformed TASKSTATS protocol"),
        ]:
            with self.subTest(error=type(error).__name__):
                sock = TaskstatsSocket()
                sock.error = error
                with self.assertRaisesRegex(VerificationError, diagnostic):
                    self.read(sock)
                self.assertTrue(sock.closed)

    def test_netlink_error_is_not_a_successful_reply(self):
        sock = TaskstatsSocket()
        sock.replies[0] = struct.pack("=IHHIIi", 20, 2, 0, 1, 1234, -1)
        with self.assertRaisesRegex(VerificationError, "CAP_NET_ADMIN"):
            self.read(sock)

    def test_truncated_wrong_sender_and_wrong_sequence_fail_closed(self):
        for failure in ["truncated", "sender", "sequence", "length", "multipart"]:
            with self.subTest(failure=failure):
                sock = TaskstatsSocket()
                if failure == "truncated":
                    sock.flags = 32
                elif failure == "sender":
                    sock.sender = (123, 0)
                else:
                    message = bytearray(sock.replies[0])
                    offset, fmt, value = {
                        "sequence": (8, "=I", 99),
                        "length": (0, "=I", 9999),
                        "multipart": (6, "=H", 2),
                    }[failure]
                    struct.pack_into(fmt, message, offset, value)
                    sock.replies[0] = bytes(message)
                with self.assertRaises(VerificationError):
                    self.read(sock)
                self.assertTrue(sock.closed)

    def test_wrong_process_short_stats_and_duplicate_attributes_fail_closed(self):
        wrong = TaskstatsSocket(pid=102)
        short = TaskstatsSocket()
        short.replies[1] = short.packet(
            40,
            2,
            2,
            short.attr(
                4, short.attr(1, struct.pack("=I", 101)) + short.attr(3, bytes(151))
            ),
        )
        duplicate = TaskstatsSocket()
        duplicate.replies[0] = duplicate.packet(
            16, 1, 1, duplicate.attr(1, struct.pack("=H", 40)) * 2
        )
        for sock in [wrong, short, duplicate]:
            with self.assertRaises(VerificationError):
                self.read(sock)

    def test_backward_clock_and_impossible_elapsed_time_fail_closed(self):
        for times in [(30_000_000_000, 29_000_000_000), (1, 2)]:
            with (
                self.subTest(times=times),
                self.assertRaisesRegex(VerificationError, "timing"),
            ):
                self.read(TaskstatsSocket(), times)


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
        self.births = {101: (19_999_999, 20_000_100)}

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

    def rows(self, *, start=19_995_000, messages=None):
        return [
            {
                "_BOOT_ID": self.boot.replace("-", ""),
                "_SYSTEMD_INVOCATION_ID": self.invocation,
                "_PID": "100",
                "__MONOTONIC_TIMESTAMP": str(start + i * 10000),
                "MESSAGE": message,
            }
            for i, message in enumerate(self.messages if messages is None else messages)
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
        raise AssertionError(f"unexpected command {argv}")

    @contextmanager
    def journal(self, boot, invocation, pid, since):
        self.journal_reads = 0
        self.journal_closed = False
        self.journal_query = (boot, invocation, pid, since)

        def stream():
            rows = self.rows_override if self.rows_override is not None else self.rows()
            for row in rows:
                if int(row["__MONOTONIC_TIMESTAMP"]) < since:
                    continue
                self.journal_reads += 1
                yield row

        try:
            yield stream()
        finally:
            self.journal_closed = True

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
            task_start=lambda pid: self.births[pid],
            journal_open=self.journal,
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

    def test_previous_same_argv_and_port_do_not_make_current_start_ambiguous(self):
        current = self.host.rows()
        # Cover both distant history and a completed prior load still within
        # the five-second pre-spawn marker allowance for the current process.
        for previous_start in [1_000_000, 19_000_000]:
            with self.subTest(previous_start=previous_start):
                self.host.rows_override = self.host.rows(start=previous_start) + current
                evidence = self.verify()["models"][0]
                self.assertEqual(evidence["start_ticks"], 2000)
                self.assertEqual(evidence["gpu_layers"], 43)

    def test_malformed_historical_command_is_not_current_identity(self):
        previous = self.host.rows(start=1_000_000)
        previous[0]["MESSAGE"] = 'msg="starting llama-server" cmd="unterminated'
        self.host.rows_override = previous + self.host.rows()
        self.verify()

    def test_far_earlier_unfinished_startup_does_not_poison_current_proof(self):
        self.host.rows_override = self.host.rows(start=1_000_000)[:1] + self.host.rows()
        self.verify()

    def test_earlier_bad_runtime_evidence_does_not_taint_current_load(self):
        previous = self.host.rows(start=19_000_000)
        for row in previous:
            row["MESSAGE"] = (
                row["MESSAGE"]
                .replace("Vulkan0", "ROCm0")
                .replace("43/43", "1/43")
                .replace("K (q8_0)", "K (f16)")
            )
        self.host.rows_override = previous + self.host.rows()
        self.verify()

    def test_prior_good_load_cannot_rescue_bad_current_runtime_evidence(self):
        previous = self.host.rows(start=19_000_000)
        for before, after in [
            ("Vulkan0", "ROCm0"),
            ("AMD Radeon RX 9070 XT", "AMD Radeon Graphics"),
            ("43/43", "1/43"),
            ("K (q8_0)", "K (f16)"),
            ("V (q8_0)", "V (f16)"),
        ]:
            with self.subTest(before=before):
                current = self.host.rows()
                for row in current:
                    row["MESSAGE"] = row["MESSAGE"].replace(before, after)
                self.host.rows_override = previous + current
                with self.assertRaises(VerificationError):
                    self.verify()

    def test_stale_only_startup_is_not_current_even_inside_launch_allowance(self):
        self.host.rows_override = self.host.rows(start=19_000_000)
        with self.assertRaisesRegex(
            VerificationError, "current runner startup interval"
        ):
            self.verify()

    def test_missing_current_marker_cannot_reuse_prior_same_command(self):
        self.host.rows_override = (
            self.host.rows(start=19_000_000) + self.host.rows()[1:]
        )
        with self.assertRaisesRegex(
            VerificationError, "current runner startup interval"
        ):
            self.verify()

    def test_current_command_mismatch_is_not_rescued_by_historical_match(self):
        current = self.host.rows()
        current[0]["MESSAGE"] = current[0]["MESSAGE"].replace("43210", "43211")
        self.host.rows_override = self.host.rows(start=1_000_000) + current
        with self.assertRaisesRegex(
            VerificationError, "current startup command differs"
        ):
            self.verify()

    def test_two_start_markers_compatible_with_birth_bounds_are_ambiguous(self):
        current = self.host.rows()
        duplicate = {**current[0], "__MONOTONIC_TIMESTAMP": "20000000"}
        self.host.rows_override = current[:1] + [duplicate] + current[1:]
        with self.assertRaisesRegex(
            VerificationError, "ambiguous current runner startup interval"
        ):
            self.verify()

    def test_stale_fields_before_proc_start_cannot_complete_current_proof(self):
        # Move each observation before the first possible process timestamp,
        # while preserving the same current command and listening marker.
        for index in [1, 2, 3, 4, 5, 6, 7, 8, 9]:
            with self.subTest(index=index):
                current = self.host.rows()
                current[index]["__MONOTONIC_TIMESTAMP"] = "19999000"
                self.host.rows_override = sorted(
                    current, key=lambda row: int(row["__MONOTONIC_TIMESTAMP"])
                )
                with self.assertRaises(VerificationError):
                    self.verify()

    def test_later_listening_output_does_not_change_completed_startup(self):
        current = self.host.rows()
        other = {
            **current[-1],
            "MESSAGE": current[-1]["MESSAGE"].replace("43210", "43211"),
        }
        self.host.rows_override = current + [other]
        self.verify()
        self.assertEqual(self.host.journal_reads, len(current))

    def test_ambiguous_first_listening_event_fails_closed(self):
        self.host.messages[-1] += " listening on http://127.0.0.1:43211"
        with self.assertRaisesRegex(
            VerificationError, "ambiguous runner listening marker"
        ):
            self.verify()

    def test_later_traffic_cannot_displace_startup(self):
        startup = self.host.rows()
        later = [
            {
                **startup[-1],
                "__MONOTONIC_TIMESTAMP": str(21_000_000 + i),
                "MESSAGE": "GET /api/ps",
                "UNUSED_METADATA": "x" * 2048,
            }
            for i in range(20001)
        ]
        self.host.rows_override = startup + later
        self.verify()
        self.assertEqual(self.host.journal_reads, len(startup))
        self.assertTrue(self.host.journal_closed)
        self.assertEqual(
            self.host.journal_query,
            (self.host.boot, self.host.invocation, 100, 14_999_999),
        )

    def test_startup_seek_skips_old_generations(self):
        self.host.rows_override = self.host.rows(start=1_000_000) + self.host.rows()
        self.verify()
        self.assertEqual(self.host.journal_reads, len(self.host.messages))

    def test_rotated_out_startup_fails_closed(self):
        self.host.rows_override = self.host.rows()[1:]
        with self.assertRaisesRegex(VerificationError, "startup interval"):
            self.verify()
        self.assertTrue(self.host.journal_closed)

    def test_only_wrong_current_listener_is_rejected(self):
        current = self.host.rows()
        current[-1]["MESSAGE"] = current[-1]["MESSAGE"].replace("43210", "43211")
        self.host.rows_override = current
        with self.assertRaisesRegex(
            VerificationError, "listener differs from runner port"
        ):
            self.verify()

    def test_mixed_current_fields_cannot_select_only_the_matching_value(self):
        for index, before, after in [
            (1, "Vulkan0", "ROCm0"),
            (3, "43/43", "1/43"),
            (4, "262144", "131072"),
            (9, "K (q8_0)", "K (f16)"),
        ]:
            with self.subTest(index=index):
                current = self.host.rows()
                other = {
                    **current[index],
                    "__MONOTONIC_TIMESTAMP": current[-1]["__MONOTONIC_TIMESTAMP"],
                    "MESSAGE": current[index]["MESSAGE"].replace(before, after),
                }
                self.host.rows_override = current[:-1] + [other] + current[-1:]
                with self.assertRaises(VerificationError):
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
        self.host.births[102] = (20_129_999, 20_130_100)
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

    def test_monotonic_birth_is_independent_of_proc_tick_rounding(self):
        self.host.births[101] = (20_006_999, 20_007_100)
        self.host.rows_override = self.host.rows(start=20_005_000)
        self.verify()

    def test_journal_marker_receipt_may_trail_process_birth_by_one_tick(self):
        # The daemon emits the marker before cmd.Start, but journald may read
        # that stream after the child exists. Its timestamp is receipt time.
        current = self.host.rows(start=20_005_000)
        self.host.rows_override = self.host.rows(start=19_000_000) + current
        self.verify()
        # A second nearby startup remains ambiguous even with identical argv
        # and a reused port; text cannot pick one of the compatible intervals.
        duplicate = {**current[0], "__MONOTONIC_TIMESTAMP": "20006000"}
        self.host.rows_override = current[:1] + [duplicate] + current[1:]
        with self.assertRaisesRegex(VerificationError, "ambiguous current runner"):
            self.verify()

    def test_receipt_delay_beyond_the_existing_tick_budget_fails_closed(self):
        self.host.rows_override = self.host.rows(start=20_015_000)
        with self.assertRaisesRegex(
            VerificationError, "current runner startup interval"
        ):
            self.verify()

    def test_suspend_before_and_after_process_birth_preserves_binding(self):
        # (daemon BOOTTIME tick, runner BOOTTIME tick) for suspend before both,
        # between their births, after both, and both before/after. Their
        # historical MONOTONIC births and journal entries do not shift.
        for daemon, runner in [(7000, 8000), (1000, 8000), (1000, 2000), (7000, 8000)]:
            with self.subTest(daemon=daemon, runner=runner):
                self.host.process(100, 1, daemon, ["/usr/local/bin/ollama", "serve"])
                self.host.process(101, 100, runner, self.host.argv)
                with self.host.runtime() as runtime:
                    sock = TaskstatsSocket()
                    runtime.task_start = lambda pid: TaskstatsTests().read(sock)
                    result = runtime.verify(self.host.expected, [self.host.model])
                self.assertEqual(result["models"][0]["start_ticks"], runner)

    def test_suspend_during_capture_preserves_current_generation(self):
        sock = TaskstatsSocket(elapsed=10_000_050)
        self.runtime.task_start = lambda pid: TaskstatsTests().read(sock)
        self.verify()

    def test_timing_source_failure_and_generation_change_close_runner_pidfd(self):
        def timing_failure(pid):
            raise VerificationError("TASKSTATS unavailable")

        def replace(pid):
            self.host.process(pid, 100, 3000, self.host.argv)
            return 19_999_999, 20_000_100

        for source, message in [
            (timing_failure, "unavailable"),
            (replace, "generation changed"),
        ]:
            with self.subTest(message=message):
                self.runtime.task_start = source
                with self.assertRaisesRegex(VerificationError, message):
                    self.verify()
                self.assertIn(1001, self.host.closed)
                self.assertNotIn(1000, self.host.closed)

    def test_exited_runner_cannot_supply_taskstats_for_reused_pid(self):
        def exited(pid):
            self.runtime.wait_exit = lambda fd, timeout: fd == 1001
            return 19_999_999, 20_000_100

        self.runtime.task_start = exited
        with self.assertRaisesRegex(
            VerificationError, "generation changed during TASKSTATS"
        ):
            self.verify()

    def test_slow_or_uncertain_timing_cannot_select_a_startup(self):
        self.host.births[101] = (19_980_000, 20_000_100)
        with self.assertRaisesRegex(VerificationError, "ambiguous TASKSTATS birth"):
            self.verify()
        self.host.births[101] = (20_110_000, 20_116_000)
        with self.assertRaisesRegex(VerificationError, "ambiguous runner birth"):
            self.verify()

    def test_slow_samples_retry_same_generation_until_one_precise_sample(self):
        for slow_count in (1, 2):
            with self.subTest(slow_count=slow_count):
                samples = [(19_980_000, 20_000_100)] * slow_count + [
                    (19_999_999, 20_000_100)
                ]
                calls, opened = [], []
                closed_before = len(self.host.closed)

                def sample(pid):
                    calls.append(pid)
                    return samples.pop(0)

                def open_fd(pid, flags):
                    opened.append((pid, flags))
                    return 1001

                self.runtime.task_start = sample
                self.runtime.open_fd = open_fd
                evidence = self.verify()["models"][0]
                self.assertEqual(calls, [101] * (slow_count + 1))
                self.assertEqual(opened, [(101, 0)])
                self.assertEqual(self.host.closed[closed_before:], [1001])
                self.assertEqual(
                    evidence["birth_monotonic_us"],
                    {"lower": 19_999_999, "upper": 20_000_100},
                )

    def test_three_uncertain_samples_fail_without_combining_birth_bounds(self):
        # Their intersection would be precise enough. Each independent
        # observation is still too wide, so none can certify the runner.
        samples = iter(
            [
                (19_990_000, 20_010_000),
                (19_999_000, 20_019_000),
                (19_981_000, 20_001_000),
            ]
        )
        calls = []

        def sample(pid):
            calls.append(pid)
            return next(samples)

        self.runtime.task_start = sample
        with self.assertRaisesRegex(VerificationError, "after 3 samples") as caught:
            self.verify()
        self.assertEqual(calls, [101] * 3)
        self.assertEqual(self.host.closed, [1001])
        self.assertNotIn("retry verification", str(caught.exception))

    def test_generation_change_between_samples_prevents_another_query(self):
        calls, checks = [], []
        original_stat = self.runtime._stat

        def stat(pid):
            checks.append(pid)
            if len(checks) == 3:
                self.host.process(101, 100, 3000, self.host.argv)
            return original_stat(pid)

        def sample(pid):
            calls.append(pid)
            return 19_980_000, 20_000_100

        self.runtime._stat = stat
        self.runtime.task_start = sample
        with self.assertRaisesRegex(VerificationError, "generation changed before"):
            self.runtime._runner_start(101, {"start": 2000})
        self.assertEqual(calls, [101])
        self.assertEqual(self.host.closed, [1001])

    def test_retained_pidfd_exit_between_samples_prevents_pid_reuse(self):
        polls, calls = [], []

        def exited(fd, timeout):
            self.assertEqual(fd, 1001)
            polls.append(fd)
            return len(polls) == 3

        def sample(pid):
            calls.append(pid)
            return 19_980_000, 20_000_100

        self.runtime.wait_exit = exited
        self.runtime.task_start = sample
        with self.assertRaisesRegex(VerificationError, "generation changed before"):
            self.runtime._runner_start(101, {"start": 2000})
        self.assertEqual(calls, [101])
        self.assertEqual(self.host.closed, [1001])

    def test_protocol_or_access_failure_after_slow_sample_is_terminal(self):
        for failure in ("protocol", "permission", "timeout"):
            with self.subTest(failure=failure):
                calls = []
                sock = TaskstatsSocket()
                if failure == "protocol":
                    sock.replies[0] = b"invalid packet"
                else:
                    sock.error = (
                        PermissionError() if failure == "permission" else TimeoutError()
                    )

                def sample(pid):
                    calls.append(pid)
                    if len(calls) == 1:
                        return 19_980_000, 20_000_100
                    return TaskstatsTests().read(sock)

                self.runtime.task_start = sample
                with self.assertRaises(VerificationError) as caught:
                    self.verify()
                self.assertEqual(calls, [101, 101])
                self.assertNotIn("retry verification", str(caught.exception))
                self.assertTrue(sock.closed)

    def test_invalid_birth_bounds_do_not_retry(self):
        for bounds in [(-1, 20_000_100), (20_000_100, 20_000_000)]:
            with self.subTest(bounds=bounds):
                calls = []

                def sample(pid):
                    calls.append(pid)
                    return bounds

                self.runtime.task_start = sample
                with self.assertRaisesRegex(VerificationError, "invalid TASKSTATS"):
                    self.verify()
                self.assertEqual(calls, [101])

    def test_cancellation_after_slow_sample_propagates_and_closes_pidfd(self):
        class Cancelled(BaseException):
            pass

        cancelled = Cancelled("stop")
        calls = []

        def sample(pid):
            calls.append(pid)
            if len(calls) == 1:
                return 19_980_000, 20_000_100
            raise cancelled

        self.runtime.task_start = sample
        with self.assertRaises(Cancelled) as caught:
            self.verify()
        self.assertIs(caught.exception, cancelled)
        self.assertEqual(calls, [101, 101])
        self.assertEqual(self.host.closed, [1001])

    def test_real_cancellation_at_descriptor_boundaries_releases_ownership(self):
        # Each signal runs in its own process: faulty mask restoration cannot
        # affect the suite. No real pidfds, netlink or Ollama calls are made.
        script = r"""
import runpy
import signal
import sys
import tempfile

fixtures = runpy.run_path(sys.argv[1], run_name="runtime_fixtures")
Host, Socket, Reader = (fixtures[name] for name in ("Host", "TaskstatsSocket", "TaskstatsTests"))
runtime_module, signals = (sys.modules[name] for name in ("ollama_runtime", "ollama_signals"))
phase, signum = sys.argv[2], getattr(signal, sys.argv[3])

class Cancelled(BaseException):
    pass

def cancel(signum, frame):
    raise Cancelled(signum)

signal.signal(signum, cancel)
signal.pthread_sigmask(signal.SIG_UNBLOCK, {signum})
initial_mask = signal.pthread_sigmask(signal.SIG_BLOCK, set())

@signals.deferred_context(critical=False)
def transaction():
    yield

with tempfile.TemporaryDirectory() as directory:
    host, sock = Host(directory), Socket()
    runtime = None if phase.startswith("capture") else host.runtime()
    if runtime:
        runtime.task_start = lambda pid: Reader().read(sock)
    fired, signal_returned = False, False
    capture_body = runtime_module.Runtime._capture_fd.__wrapped__

    def trace(frame, event, arg):
        global fired, signal_returned
        matched = False
        if phase == "capture_body_return":
            matched = event == "return" and frame.f_code is capture_body.__code__
        elif phase == "capture_helper_return":
            matched = (event == "return" and frame.f_code.co_name == "run"
                       and frame.f_locals.get("function") is capture_body)
        elif event == "call":
            if phase in {"runner_enter", "runner_exit", "socket_enter", "socket_exit"}:
                method = "__enter__" if phase.endswith("enter") else "__exit__"
                if frame.f_code is getattr(signals._DeferredContext, method).__code__:
                    generator = frame.f_locals["self"].gen
                    if generator and generator.gi_code.co_name == "owned_descriptor":
                        runner = generator.gi_frame.f_locals["opener"] is runtime.open_fd
                        matched = runner == phase.startswith("runner")
            elif phase == "socket_exchange":
                matched = frame.f_code is Socket.recvmsg.__code__
            elif phase == "systemctl":
                matched = frame.f_code is Host.command.__code__
            elif phase == "http":
                matched = frame.f_code is Host.http.__code__
        if matched and not fired:
            fired = True
            signal.raise_signal(signum)
            signal_returned = True
        return trace

    cancelled = False
    try:
        sys.settrace(trace)
        if phase.startswith("capture"):
            host.runtime()
        else:
            # Model the CLI's masked outer ownership transaction. Only the
            # explicit bounded observation calls should enable delivery.
            with transaction():
                runtime.verify(host.expected, [host.model])
    except Cancelled:
        cancelled = True
    finally:
        sys.settrace(None)
        if runtime:
            runtime.close()
    assert fired and cancelled, (phase, fired, cancelled)
    assert host.closed.count(1000) == 1, host.closed
    expected_runner_closes = 0 if phase.startswith("capture") or phase == "systemctl" else 1
    assert host.closed.count(1001) == expected_runner_closes, host.closed
    if phase.startswith("socket") or phase in {"http", "runner_exit"}:
        assert sock.closed
    if phase in {"socket_exchange", "systemctl", "http"}:
        assert not signal_returned, "bounded observation masked cancellation"
    assert signal.pthread_sigmask(signal.SIG_BLOCK, set()) == initial_mask
"""
        for phase in (
            "capture_body_return",
            "capture_helper_return",
            "runner_enter",
            "runner_exit",
            "socket_enter",
            "socket_exit",
            "socket_exchange",
            "systemctl",
            "http",
        ):
            for signum in ("SIGTERM", "SIGINT"):
                with self.subTest(phase=phase, signum=signum):
                    result = subprocess.run(
                        [sys.executable, "-c", script, __file__, phase, signum],
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    self.assertEqual(
                        result.returncode, 0, result.stdout + result.stderr
                    )

    def test_stale_fields_inside_uncertain_birth_bounds_are_not_evidence(self):
        self.host.births[101] = (20_000_000, 20_008_000)
        # Device line at 20.005s could precede process birth; exclude it.
        with self.assertRaisesRegex(VerificationError, "GPU device"):
            self.verify()

    def test_shifted_or_unreadable_time_namespace_fails_closed(self):
        self.host.write("self/ns/time", "namespace")
        with self.assertRaisesRegex(
            VerificationError, "missing monotonic time namespace"
        ):
            self.verify()
        self.host.write("self/timens_offsets", "monotonic 60 0\nboottime 60 0\n")
        with self.assertRaisesRegex(VerificationError, "unshifted monotonic"):
            self.verify()
        self.host.write("self/timens_offsets", "monotonic 0 0\nboottime 60 0\n")
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
