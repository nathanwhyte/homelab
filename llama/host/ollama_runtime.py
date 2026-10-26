"""Linux runtime evidence for Ollama 0.40.11 (llama.cpp b11845).

This adapter observes an already loaded daemon; it never loads a model. The
caller supplies recipe/API evidence and serializes preparation and loading.
Missing, stale or ambiguous observations are failures, including journal loss.

Runner text is forwarded through the daemon without a runner PID. Attribution
therefore requires an ordered, retained startup interval consistent with proc
generation, monotonic birth time, exact argv, and /props; it is not an
independently identified runner journal stream. Only that current interval
supplies runtime observations.

Sources: ollama/ollama v0.40.11 llm/llama_server.go (runner argv, forwarded
stderr), server/routes.go (SIGINT/SIGTERM handling); ggml-org/llama.cpp b11845
tools/server/server-context.cpp (/props), src/llama-{context,model,kv-cache}.cpp
(actual startup values). Updating the supported version requires new fixtures.
"""

import json
import os
from pathlib import Path
import pwd
import re
import select
import signal
import socket
import struct
import subprocess
import time
import urllib.request

from ollama_signals import deferred, interruptible, owned_descriptor


class VerificationError(RuntimeError):
    """The declared posture could not be proved for the captured generation."""


SERVICE = "ollama.service"
_PROPERTIES = {
    "MainPID",
    "InvocationID",
    "ControlGroup",
    "User",
    "KillMode",
    "RestartPreventExitStatus",
    "RestartForceExitStatus",
    "SendSIGKILL",
    "TimeoutStopUSec",
}
_MAX_BYTES = 16 * 1024 * 1024


def _netlink_attributes(data):
    """Decode the small, non-repeating generic-netlink attribute set we use."""
    values = {}
    while data:
        if len(data) < 4:
            raise VerificationError("truncated TASKSTATS attribute")
        length, kind = struct.unpack_from("=HH", data)
        padded = (length + 3) & ~3
        if kind & 0x4000:  # NLA_F_NET_BYTEORDER is not used by this ABI.
            raise VerificationError("unexpected TASKSTATS byte order")
        kind &= 0x3FFF  # NLA_F_NESTED is not a type bit.
        if length < 4 or padded > len(data) or kind in values:
            raise VerificationError("malformed or duplicate TASKSTATS attribute")
        values[kind] = data[4:length]
        data = data[padded:]
    return values


def _task_start(pid, *, socket_factory=None, monotonic_ns=None):
    """Bound process birth in CLOCK_MONOTONIC microseconds via TASKSTATS.

    Linux v6.17 kernel/tsacct.c computes ac_etime from ktime_get_ns() minus
    task->start_time, then floors to microseconds. Unlike proc stat's
    start_boottime, this clock excludes suspend. Sample on both sides of the
    request: even a suspend during the exchange needs no historical offset.
    The caller holds a pidfd and checks proc generation around this PID query.
    """
    factory = socket_factory or socket.socket
    clock = monotonic_ns or time.monotonic_ns

    def attribute(kind, value):
        length = len(value) + 4
        return struct.pack("=HH", length, kind) + value + b"\0" * (-length % 4)

    def exchange(sock, family, command, sequence, attrs):
        body = struct.pack("=BBH", command, 1, 0) + attrs
        message = struct.pack("=IHHII", len(body) + 16, family, 1, sequence, 0) + body
        sock.sendto(message, (0, 0))
        data, _, flags, sender = sock.recvmsg(65536)
        if flags or sender != (0, 0) or len(data) < 20:
            raise VerificationError("truncated or non-kernel TASKSTATS response")
        length, kind, reply_flags, seq, _ = struct.unpack_from("=IHHII", data)
        if length != len(data) or seq != sequence or reply_flags & 2:
            raise VerificationError("malformed TASKSTATS response envelope")
        if kind == 2:  # NLMSG_ERROR: no ACK was requested.
            errno = -struct.unpack_from("=i", data, 16)[0]
            raise OSError(errno, "TASKSTATS request rejected")
        if kind != family:
            raise VerificationError("unexpected TASKSTATS response family")
        reply_command, _, reserved = struct.unpack_from("=BBH", data, 16)
        if reply_command != (1 if family == 16 else 2) or reserved:
            raise VerificationError("unexpected TASKSTATS response command")
        return _netlink_attributes(data[20:])

    try:
        # AF_NETLINK=16 and NETLINK_GENERIC=16 are Linux ABI values. Keeping
        # these here lets byte fixtures run on development hosts without Linux.
        with owned_descriptor(
            factory, 16, socket.SOCK_RAW, 16, closer=lambda sock: sock.close()
        ) as sock:
            sock.settimeout(5)
            sock.bind((0, 0))
            control = interruptible(
                exchange, sock, 16, 3, 1, attribute(2, b"TASKSTATS\0")
            )
            if len(control.get(1, b"")) != 2:
                raise VerificationError("missing TASKSTATS family identity")
            family = struct.unpack("=H", control[1])[0]
            if family <= 16:
                raise VerificationError("invalid TASKSTATS family identity")
            before = clock()
            response = interruptible(
                exchange, sock, family, 1, 2, attribute(1, struct.pack("=I", pid))
            )
            after = clock()
        # TASKSTATS_TYPE_AGGR_PID contains TYPE_PID and TYPE_STATS. The basic
        # accounting prefix has fixed, explicitly aligned offsets (UAPI
        # linux/taskstats.h); later versions append fields without moving it.
        nested = _netlink_attributes(response.get(4, b""))
        stats = nested.get(3, b"")
        if (
            nested.get(1) != struct.pack("=I", pid)
            or len(stats) < 152
            or struct.unpack_from("=H", stats)[0] < 2
            or struct.unpack_from("=I", stats, 128)[0] != pid
        ):
            raise VerificationError("missing or mismatched TASKSTATS process identity")
        elapsed = struct.unpack_from("=Q", stats, 144)[0]
        # ac_etime rounds down; expand outwards so the whole possible birth
        # interval is retained. Do not turn observation latency into precision.
        lower = (before - (elapsed + 1) * 1000) // 1000
        upper = (after - elapsed * 1000 + 999) // 1000
        if before > after or lower < 0 or upper < lower:
            raise VerificationError("invalid TASKSTATS monotonic timing")
        return lower, upper
    except (OSError, ValueError, struct.error) as exc:
        raise VerificationError(
            "TASKSTATS monotonic process timing unavailable; use a kernel with "
            "CONFIG_TASKSTATS and run the verifier as root with CAP_NET_ADMIN"
        ) from exc


def _command(argv):
    try:
        result = subprocess.run(
            argv, check=True, capture_output=True, text=True, timeout=20
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # Do not include command output: service environments can contain secrets.
        raise VerificationError(f"{argv[0]} observation failed") from exc
    if len(result.stdout) > _MAX_BYTES:
        raise VerificationError("observation exceeded size limit")
    return result.stdout


def _http_get(port):
    try:
        # A runner observation must never use the caller's HTTP proxy settings.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/props", timeout=5) as response:
            body = response.read(256 * 1024 + 1)
        if len(body) > 256 * 1024:
            raise VerificationError("runner /props exceeded size limit")
        return json.loads(body)
    except (OSError, ValueError) as exc:
        raise VerificationError("runner /props unavailable") from exc


def _wait_exit(fd, timeout):
    poller = select.poll()
    poller.register(fd, select.POLLIN)
    return bool(poller.poll(int(timeout * 1000)))


def _one(values, label):
    if len(values) != 1:
        raise VerificationError(f"missing or ambiguous {label}")
    return values[0]


def _nul_fields(data):
    if not data or not data.endswith(b"\0"):
        raise VerificationError("malformed proc NUL record")
    try:
        return data[:-1].decode("utf-8").split("\0")
    except UnicodeError as exc:
        raise VerificationError("non-UTF8 proc record") from exc


def _environment(data):
    env = {}
    for entry in _nul_fields(data):
        key, sep, value = entry.partition("=")
        if not sep or not key or key in env:
            raise VerificationError("malformed or duplicate process environment")
        env[key] = value
    return env


def _flag(argv, name):
    # Only accept the exact v0.40.11 argv form. An alias or duplicate cannot
    # silently override one of the values being certified.
    positions = [i for i, value in enumerate(argv) if value == name]
    index = _one(positions, f"runner flag {name}")
    if index + 1 >= len(argv):
        raise VerificationError(f"missing runner value {name}")
    return argv[index + 1]


def _gpu_description_matches(description, expected, backend):
    if description == expected:
        return True
    # b11845 ggml-vulkan.cpp passes VkPhysicalDeviceProperties.deviceName
    # through unchanged. Mesa 26.2.2 radv_physical_device.c constructs that
    # from the exact marketing name plus " (RADV <family>[ (LLVM <version>)])".
    # Accept only that complete annotation: never strip arbitrary parentheses
    # or search for a hardware name inside another device's description.
    suffix = r" \(RADV [A-Z][A-Z0-9_]*(?: \(LLVM [0-9]+(?:\.[0-9]+){1,2}[A-Za-z0-9.+~-]*\))?\)"
    return (
        backend == "Vulkan"
        and re.fullmatch(re.escape(expected) + suffix, description) is not None
    )


class Runtime:
    """Capture a pidfd once; verify and stop only that daemon generation.

    Dependency injection is for hermetic tests. Production uses systemctl,
    journalctl, procfs and loopback /props. No fallback to kill(PID) exists.
    Keep this object open through the caller's adoption decision; call close()
    afterward, or use it as a context manager.
    """

    def __init__(
        self,
        *,
        proc_root="/proc",
        run=None,
        http_get=None,
        pidfd_open=None,
        pidfd_send_signal=None,
        wait_exit=None,
        close_fd=None,
        user_uid=None,
        clock_ticks=None,
        task_start=None,
    ):
        self.proc = Path(proc_root)
        self.run = run or _command
        self.http_get = http_get or _http_get
        self.send_signal = pidfd_send_signal or getattr(
            signal, "pidfd_send_signal", None
        )
        self.wait_exit = wait_exit or _wait_exit
        self.close_fd = close_fd or os.close
        self.user_uid = user_uid or (lambda name: pwd.getpwnam(name).pw_uid)
        self.ticks = clock_ticks or os.sysconf("SC_CLK_TCK")
        self.task_start = task_start or _task_start
        self.fd = None
        open_fd = pidfd_open or getattr(os, "pidfd_open", None)
        self.open_fd = open_fd
        if open_fd is None or self.send_signal is None:
            raise VerificationError("Linux pidfd support is required")
        try:
            self.boot_id = self._read("sys/kernel/random/boot_id").decode().strip()
            if not re.fullmatch(r"[0-9a-f-]{36}", self.boot_id):
                raise VerificationError("invalid kernel boot identity")
            props = self._properties()
            self.pid = int(props["MainPID"])
            self.invocation = props["InvocationID"]
            if self.pid <= 1 or not re.fullmatch(r"[0-9a-f]{32}", self.invocation):
                raise VerificationError("Ollama has no identifiable running generation")
            self.start = self._stat(self.pid)[1]
            self._capture_fd()
            self._assert_current()
        except VerificationError:
            self.close()
            raise
        except (OSError, ValueError, KeyError) as exc:
            self.close()
            raise VerificationError("cannot capture Ollama generation") from exc
        except BaseException:
            # Deferred acquisition may deliver pending cancellation only after
            # storing the fd. Constructor failure must release that ownership.
            self.close()
            raise

    @deferred
    def _capture_fd(self):
        self.fd = self.open_fd(self.pid, 0)

    @deferred
    def close(self):
        fd, self.fd = self.fd, None
        if fd is not None:
            self.close_fd(fd)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _read(self, path):
        try:
            data = (self.proc / str(path)).read_bytes()
        except OSError as exc:
            raise VerificationError(f"cannot read proc observation {path}") from exc
        if len(data) > _MAX_BYTES:
            raise VerificationError("proc observation exceeded size limit")
        return data

    def _stat(self, pid):
        # comm may itself contain spaces and closing parentheses.
        try:
            text = self._read(f"{pid}/stat").decode()
            fields = text[text.rindex(")") + 2 :].split()
            return int(fields[1]), int(fields[19])
        except (ValueError, IndexError) as exc:
            raise VerificationError("malformed process identity") from exc

    def _properties(self, extra=()):
        names = sorted(_PROPERTIES | set(extra))
        raw = interruptible(
            self.run,
            [
                "systemctl",
                "show",
                "--no-pager",
                "--all",
                "--property=" + ",".join(names),
                SERVICE,
            ],
        )
        props = {}
        for line in raw.splitlines():
            key, sep, value = line.partition("=")
            if not sep or key in props:
                raise VerificationError("malformed systemctl properties")
            props[key] = value
        if not set(names) <= props.keys():
            raise VerificationError("missing systemctl properties")
        return props

    def _assert_current(self):
        if self.fd is None or self.wait_exit(self.fd, 0):
            raise VerificationError("captured Ollama generation has exited")
        props = self._properties()
        if (
            props["MainPID"] != str(self.pid)
            or props["InvocationID"] != self.invocation
            or self._stat(self.pid)[1] != self.start
            or self._read("sys/kernel/random/boot_id").decode().strip() != self.boot_id
        ):
            raise VerificationError("Ollama generation changed during verification")
        return props

    def _status(self, pid):
        data = {}
        for line in self._read(f"{pid}/status").decode().splitlines():
            key, sep, value = line.partition(":")
            if sep:
                if key in data:
                    raise VerificationError("duplicate process status field")
                data[key] = value.strip()
        return data

    def _service_identity(self, pid, props):
        try:
            uid = self.user_uid("ollama")
            uids = [int(value) for value in self._status(pid)["Uid"].split()]
            exe = (self.proc / str(pid) / "exe").readlink()
        except (OSError, KeyError, ValueError) as exc:
            raise VerificationError("cannot identify Ollama process owner") from exc
        if props["User"] != "ollama" or uids != [uid] * 4:
            raise VerificationError("Ollama processes must run as User=ollama")
        cgroups = self._read(f"{pid}/cgroup").decode().splitlines()
        if "0::" + props["ControlGroup"] not in cgroups or not props["ControlGroup"]:
            raise VerificationError("process is outside the daemon control group")
        return exe

    def _stop_policy(self, props):
        # systemctl renders signal names without SIG (e.g. HUP, not SIGHUP).
        if (
            "HUP" not in props["RestartPreventExitStatus"].split()
            or props["RestartForceExitStatus"]
            or props["KillMode"] != "control-group"
            or props["SendSIGKILL"] != "yes"
            or props["TimeoutStopUSec"] in {"", "infinity", "0"}
        ):
            raise VerificationError(
                "daemon does not have a bounded no-restart failure stop"
            )
        try:
            status = self._status(self.pid)
            # SIGHUP is bit zero. Go respects inherited SIG_IGN for HUP; a
            # blocked signal would make a bounded stop unreliable as well.
            if int(status["SigIgn"], 16) & 1 or int(status["SigBlk"], 16) & 1:
                raise VerificationError("daemon ignores or blocks failure-stop SIGHUP")
        except (ValueError, KeyError) as exc:
            raise VerificationError(
                "cannot establish daemon signal disposition"
            ) from exc

    def _runners(self, props):
        runners = {}
        for entry in self.proc.iterdir():
            if not entry.name.isdecimal():
                continue
            pid = int(entry.name)
            try:
                parent, start = self._stat(pid)
            except VerificationError:
                # Other processes may exit during enumeration. The required
                # resident set and a second scan make a lost runner fail closed.
                continue
            if parent != self.pid:
                continue
            argv = _nul_fields(self._read(f"{pid}/cmdline"))
            exe = self._service_identity(pid, props)
            if exe.name != "llama-server" or Path(argv[0]).name != "llama-server":
                raise VerificationError("unexpected daemon child process")
            runners[pid] = {"start": start, "argv": argv}
        return runners

    def _journal(self):
        raw = interruptible(
            self.run,
            [
                "journalctl",
                "--no-pager",
                "--all",
                "--output=json",
                "--lines=20000",
                f"_BOOT_ID={self.boot_id.replace('-', '')}",
                f"_SYSTEMD_INVOCATION_ID={self.invocation}",
                f"_PID={self.pid}",
            ],
        )
        rows = []
        try:
            for line in raw.splitlines():
                row = json.loads(line)
                if (
                    row["_BOOT_ID"] != self.boot_id.replace("-", "")
                    or row["_SYSTEMD_INVOCATION_ID"] != self.invocation
                    or row["_PID"] != str(self.pid)
                    or not isinstance(row["MESSAGE"], str)
                ):
                    raise VerificationError("journal identity mismatch")
                stamp = int(row["__MONOTONIC_TIMESTAMP"])
                if rows and stamp < rows[-1][0]:
                    raise VerificationError("journal monotonic time went backwards")
                rows.append((stamp, row["MESSAGE"]))
        except (ValueError, KeyError, TypeError) as exc:
            raise VerificationError("malformed journal evidence") from exc
        return rows

    def _runner_start(self, pid, runner):
        # A time namespace offsets clock_gettime but not TASKSTATS. Require the
        # verifier's monotonic clock to be the kernel clock used by journald.
        # Kernels without CONFIG_TIME_NS do not expose timens_offsets.
        offsets = self.proc / "self/timens_offsets"
        try:
            data = offsets.read_text()
        except FileNotFoundError:
            if (self.proc / "self/ns/time").exists():
                raise VerificationError("missing monotonic time namespace offsets")
            data = "monotonic 0 0\n"
        entries = [line.split() for line in data.splitlines()]
        monotonic = [
            entry[1:] for entry in entries if entry and entry[0] == "monotonic"
        ]
        if monotonic != [["0", "0"]]:
            raise VerificationError(
                "TASKSTATS requires an unshifted monotonic time namespace"
            )
        with owned_descriptor(self.open_fd, pid, 0, closer=self.close_fd) as fd:
            identity = (self.pid, runner["start"])
            # A delayed query supplies a wider interval, not a more precise
            # birth. Allow three independent samples of this retained pidfd;
            # accept only a single sample within the original one-tick bound.
            # Protocol/access failures and cancellation are not retryable.
            for _ in range(3):
                if self._stat(pid) != identity or self.wait_exit(fd, 0):
                    raise VerificationError(
                        "runner generation changed before TASKSTATS"
                    )
                lower, upper = interruptible(self.task_start, pid)
                if self.wait_exit(fd, 0) or self._stat(pid) != identity:
                    raise VerificationError(
                        "runner generation changed during TASKSTATS"
                    )
                if lower < 0 or upper < lower:
                    raise VerificationError("invalid TASKSTATS monotonic timing")
                if upper - lower <= 1_000_000 / self.ticks:
                    return lower, upper
            raise VerificationError(
                "ambiguous TASKSTATS birth timing after 3 samples; "
                "runtime observation exceeded the one-tick uncertainty bound"
            )

    def _startup_interval(self, runner, rows, birth):
        # A command/port can recur within one daemon invocation. First bind a
        # monotonic lifecycle interval to the captured process birth; only
        # then inspect its command or model. Historical command text is not an
        # identity selector and cannot veto or supply proof for this process.
        earliest, latest = birth
        tick = 1_000_000 / self.ticks
        starts = [
            index
            for index, (_, message) in enumerate(rows)
            if 'msg="starting llama-server"' in message
        ]
        listening = re.compile(r"\blistening on http://127\.0\.0\.1:(\d+)(?:\s|$)")
        candidates = []
        for position, index in enumerate(starts):
            stamp = rows[index][0]
            # The journal timestamps receipt, not the daemon's pre-fork log
            # write. Retain the existing one-tick post-birth allowance for that
            # scheduling lag, anchored at the earliest possible birth so the
            # allowance cannot grow with query latency. Keep every compatible
            # interval; command/port text cannot resolve ambiguity.
            if not earliest - 5_000_000 <= stamp < earliest + tick:
                continue
            end = starts[position + 1] if position + 1 < len(starts) else len(rows)
            ready = [
                (i, event_stamp, port)
                for i in range(index + 1, end)
                for event_stamp, message in [rows[i]]
                for port in listening.findall(message)
            ]
            # A completed earlier startup cannot belong to the new process,
            # even when its argv and port are identical and its start marker
            # happens to be inside the launch-delay allowance. An incomplete
            # interval ends at the next start, or remains open and fails below.
            ended = ready[0][1] if ready else rows[end][0] if end < len(rows) else None
            if ended is None or ended >= earliest:
                candidates.append((index, ready))
        index, ready = _one(candidates, "current runner startup interval")
        match = re.search(r'\bcmd=("(?:[^"\\]|\\.)*")', rows[index][1])
        if not match:
            raise VerificationError("malformed runner startup marker")
        try:
            command = json.loads(match[1])
        except ValueError as exc:
            raise VerificationError("malformed runner command marker") from exc
        if command != " ".join(runner["argv"]):
            raise VerificationError("current startup command differs from runner argv")
        ready_index, ready_stamp, port = _one(ready, "runner listening marker")
        if ready_stamp < latest:
            raise VerificationError("ambiguous runner birth within startup evidence")
        if port != _flag(runner["argv"], "--port"):
            raise VerificationError("current startup listener differs from runner port")
        # All downstream assertions share this one interval. In particular, a
        # stale GPU/cache/offload line logged before this process existed must
        # never fill in a missing current observation.
        return "\n".join(
            message
            for stamp, message in rows[index + 1 : ready_index + 1]
            if stamp >= latest
        )

    def _startup(self, runner, rows, model, expected, birth):
        argv = runner["argv"]
        text = self._startup_interval(runner, rows, birth)
        path = _flag(argv, "--model")
        if (
            len(
                re.findall(
                    r"loaded meta data with .* from "
                    + re.escape(path)
                    + r" \(version ",
                    text,
                )
            )
            != 1
        ):
            raise VerificationError("startup evidence lacks the exact loaded weights")
        devices = re.findall(
            r"using device (\S+) \((.*?)\) \((.*?)\) - \d+ MiB free", text
        )
        device, name, pci = _one(devices, "loaded GPU device")
        if not re.fullmatch(
            re.escape(expected["backend"]) + r"\d+", device
        ) or not _gpu_description_matches(
            name, expected["gpu_name"], expected["backend"]
        ):
            raise VerificationError(
                "runner backend or GPU differs from declared posture"
            )
        offload = _one(
            re.findall(r"load_tensors: offloaded (\d+)/(\d+) layers to GPU", text),
            "layer offload",
        )
        if int(offload[0]) <= 0 or offload[0] != offload[1]:
            raise VerificationError("runner did not fully offload model layers")
        actual = {}
        for key, wanted in {
            "n_ctx": model["context"] * expected["parallel"],
            "n_ctx_seq": model["context"],
            "n_batch": model["batch"],
            "n_ubatch": model["batch"],
        }.items():
            value = _one(
                re.findall(r"llama_context: " + key + r"\s*=\s*(\d+)\s*$", text, re.M),
                key,
            )
            if int(value) != wanted:
                raise VerificationError(f"actual runner {key} differs from recipe")
            actual[key] = int(value)
        if (
            _one(
                re.findall(r"llama_context: flash_attn\s*=\s*(\w+)\s*$", text, re.M),
                "flash attention",
            )
            != "enabled"
        ):
            raise VerificationError("actual runner flash attention is not enabled")
        caches = re.findall(r"llama_kv_cache: .*K \(([^)]+)\): .*V \(([^)]+)\):", text)
        cache = expected["environment"]["OLLAMA_KV_CACHE_TYPE"]
        if not caches or any(k != cache or v != cache for k, v in caches):
            raise VerificationError("actual runner KV cache differs from posture")
        return {
            **actual,
            "device": device,
            "gpu_name": name,
            "pci_id": pci,
            "gpu_layers": int(offload[0]),
            "total_layers": int(offload[1]),
            "flash_attention": "enabled",
            "cache_type": cache,
        }

    def verify(self, expected, models):
        """Return bounded evidence, or raise without publishing an adoption claim."""
        try:
            return self._verify(expected, models)
        except VerificationError:
            raise
        except (OSError, ValueError, TypeError, KeyError, IndexError) as exc:
            raise VerificationError(
                "runtime observation is incomplete or malformed"
            ) from exc

    def _verify(self, expected, models):
        self._assert_current()
        props = self._properties(expected["service_properties"])
        for key, value in expected["service_properties"].items():
            if props[key] != str(value):
                raise VerificationError(f"effective service property differs: {key}")
        if self._service_identity(self.pid, props).name != "ollama":
            raise VerificationError("daemon executable is not Ollama")
        if _nul_fields(self._read(f"{self.pid}/cmdline"))[1:] != ["serve"]:
            raise VerificationError("unexpected daemon invocation")
        self._stop_policy(props)
        env = _environment(self._read(f"{self.pid}/environ"))
        for key, value in expected["environment"].items():
            if env.get(key) != str(value):
                raise VerificationError(f"actual daemon environment differs: {key}")
        if any(key in env for key in expected["forbidden_environment"]):
            raise VerificationError("forbidden daemon environment setting is present")
        if env.get("OLLAMA_NUM_PARALLEL") != str(expected["parallel"]) or env.get(
            "OLLAMA_MAX_LOADED_MODELS"
        ) != str(expected["max_loaded"]):
            raise VerificationError("actual scheduler limits differ from posture")
        if not models or len(models) > expected["max_loaded"]:
            raise VerificationError("invalid expected resident model count")
        runners = self._runners(props)
        if len(runners) != len(models):
            raise VerificationError("runner count differs from intended resident set")
        rows = self._journal()
        result = []
        seen = set()
        for model in models:
            sha = model["weights_sha256"].removeprefix("sha256:")
            if not re.fullmatch(r"[0-9a-f]{64}", sha):
                raise VerificationError("invalid expected weights digest")
            model_path = str(Path(env["OLLAMA_MODELS"]) / "blobs" / ("sha256-" + sha))
            pid, runner = _one(
                [
                    (pid, item)
                    for pid, item in runners.items()
                    if _flag(item["argv"], "--model") == model_path
                ],
                "runner bound to expected weights",
            )
            if pid in seen:
                raise VerificationError("multiple model identities share one runner")
            seen.add(pid)
            birth = self._runner_start(pid, runner)
            argv = runner["argv"]
            values = {
                "-c": model["context"] * expected["parallel"],
                "-np": expected["parallel"],
                "-b": model["batch"],
                "-ub": model["batch"],
                "--flash-attn": "on",
                "--cache-type-k": env["OLLAMA_KV_CACHE_TYPE"],
                "--cache-type-v": env["OLLAMA_KV_CACHE_TYPE"],
                "--host": "127.0.0.1",
            }
            # Reject aliases/equals forms capable of overriding checked flags.
            forbidden = {
                "--ctx-size",
                "--parallel",
                "--batch-size",
                "--ubatch-size",
                "-ctk",
                "-ctv",
                "-fa",
                "-m",
                "--n-predict",
            }
            if any(
                arg.split("=", 1)[0] in forbidden
                or any(arg.startswith(flag + "=") for flag in values)
                for arg in argv
            ):
                raise VerificationError("ambiguous runner argument aliases")
            for flag, value in values.items():
                if _flag(argv, flag) != str(value):
                    raise VerificationError(f"runner argument differs: {flag}")
            port = int(_flag(argv, "--port"))
            if not 1 <= port <= 65535:
                raise VerificationError("invalid runner port")
            data = interruptible(self.http_get, port)
            if (
                data["model_path"] != model_path
                or data["total_slots"] != expected["parallel"]
                or data["default_generation_settings"]["n_ctx"] != model["context"]
            ):
                raise VerificationError("runner /props differs from recipe")
            result.append(
                {
                    "tag": model["tag"],
                    "digest": model["digest"],
                    "pid": pid,
                    "start_ticks": runner["start"],
                    "birth_monotonic_us": {"lower": birth[0], "upper": birth[1]},
                    "weights_sha256": sha,
                    "slots": data["total_slots"],
                    **self._startup(runner, rows, model, expected, birth),
                }
            )
        self._assert_current()
        if self._runners(props) != runners:
            raise VerificationError("runner generation changed during verification")
        return {
            "boot_id": self.boot_id,
            "invocation_id": self.invocation,
            "pid": self.pid,
            "start_ticks": self.start,
            "models": result,
        }

    def stop_failed(self, timeout=30):
        """Send SIGHUP through the retained pidfd; never target a replacement.

        Go's unregistered SIGHUP terminates by signal; SIGINT/SIGTERM instead
        return normally from Ollama Serve and would bypass signal-based restart
        prevention. A missing no-restart policy is reported, never worked around.
        """
        if self.fd is None:
            raise VerificationError("no captured generation to stop")
        if self.wait_exit(self.fd, 0):
            return {"stopped": False, "already_exited": True, "pid": self.pid}
        props = self._assert_current()
        self._stop_policy(props)
        try:
            self.send_signal(self.fd, signal.SIGHUP, None, 0)
        except ProcessLookupError:
            return {"stopped": False, "already_exited": True, "pid": self.pid}
        except OSError as exc:
            raise VerificationError(
                "could not signal captured daemon generation"
            ) from exc
        if not self.wait_exit(self.fd, timeout):
            raise VerificationError("captured daemon did not exit within stop deadline")
        return {"stopped": True, "already_exited": False, "pid": self.pid}
