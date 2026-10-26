"""Linux runtime evidence for Ollama 0.40.11 (llama.cpp b11845).

This adapter observes an already loaded daemon; it never loads a model. The
caller supplies recipe/API evidence and serializes preparation and loading.
Missing, stale or ambiguous observations are failures, including journal loss.

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
import subprocess
import urllib.request


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
        self.fd = None
        open_fd = pidfd_open or getattr(os, "pidfd_open", None)
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
            self.fd = open_fd(self.pid, 0)
            self._assert_current()
        except VerificationError:
            self.close()
            raise
        except (OSError, ValueError, KeyError) as exc:
            self.close()
            raise VerificationError("cannot capture Ollama generation") from exc

    def close(self):
        if self.fd is not None:
            self.close_fd(self.fd)
            self.fd = None

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
        raw = self.run(
            [
                "systemctl",
                "show",
                "--no-pager",
                "--all",
                "--property=" + ",".join(names),
                SERVICE,
            ]
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
        raw = self.run(
            [
                "journalctl",
                "--no-pager",
                "--all",
                "--output=json",
                "--lines=20000",
                f"_BOOT_ID={self.boot_id.replace('-', '')}",
                f"_SYSTEMD_INVOCATION_ID={self.invocation}",
                f"_PID={self.pid}",
            ]
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

    def _startup(self, runner, rows, model, expected):
        argv = runner["argv"]
        command = " ".join(argv)
        starts = []
        for index, (stamp, message) in enumerate(rows):
            if 'msg="starting llama-server"' not in message:
                continue
            match = re.search(r'\bcmd=("(?:[^"\\]|\\.)*")', message)
            if not match:
                raise VerificationError("malformed runner startup marker")
            try:
                cmd = json.loads(match[1])
            except ValueError as exc:
                raise VerificationError("malformed runner command marker") from exc
            starts.append((index, stamp, cmd))
        index, stamp, _ = _one(
            [item for item in starts if item[2] == command], "current runner startup"
        )
        proc_start = runner["start"] * 1_000_000 / self.ticks
        if not -1_000_000 / self.ticks <= proc_start - stamp <= 5_000_000:
            # The Go log precedes cmd.Start; a prior process with identical argv
            # cannot donate its startup evidence to this PID. procfs rounds
            # starttime down to a clock tick, unlike journal microseconds.
            raise VerificationError("runner startup does not match proc start time")
        end = next((i for i, _, _ in starts if i > index), len(rows))
        lines = [message for _, message in rows[index + 1 : end]]
        port = _flag(argv, "--port")
        ready = [
            i
            for i, line in enumerate(lines)
            if re.search(
                rf"\blistening on http://127\.0\.0\.1:{re.escape(port)}(?:\s|$)", line
            )
        ]
        lines = lines[: _one(ready, "runner listening marker") + 1]
        text = "\n".join(lines)
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
        if (
            not re.fullmatch(re.escape(expected["backend"]) + r"\d+", device)
            or name != expected["gpu_name"]
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
            data = self.http_get(port)
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
                    "weights_sha256": sha,
                    "slots": data["total_slots"],
                    **self._startup(runner, rows, model, expected),
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
