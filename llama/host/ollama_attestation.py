"""Read-only recovery authority from a root-owned host verification receipt.

The exporter runs as ollama. It holds the host's existing preparation lock while
reading and using the receipt, but never writes that state or controls a process.
"""

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import pwd
import re
import stat

from ollama_recovery import (
    Http,
    PreparationError,
    recover,
    validate_config,
    verified_models,
)

MAX_RECEIPT = 1024 * 1024
MAX_PROC = 64 * 1024


def _trusted(info, owner_uid, directory=False):
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if not kind(info.st_mode) or info.st_uid != owner_uid or info.st_mode & 0o022:
        raise PreparationError(
            "preparation state must be root-owned and not group/world writable"
        )


def _identity(info):
    return info.st_dev, info.st_ino


@contextmanager
def _directory(path, owner_uid):
    # O_NOFOLLOW on every component prevents a symlinked parent from redirecting
    # the receipt. Subsequent opens are relative to the held directory descriptor.
    path = Path(path).absolute()
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(path.anchor, flags)
    try:
        for part in path.parts[1:]:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        _trusted(os.fstat(fd), owner_uid, directory=True)
        yield path, fd
    finally:
        os.close(fd)


@contextmanager
def _file(directory, name, owner_uid):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        _trusted(os.fstat(fd), owner_uid)
        yield fd
    finally:
        os.close(fd)


def _read(fd, limit):
    os.lseek(fd, 0, os.SEEK_SET)
    chunks = []
    remaining = limit + 1
    while remaining:
        chunk = os.read(fd, min(remaining, 65536))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    data = b"".join(chunks)
    if len(data) > limit:
        raise PreparationError("verification observation exceeded size limit")
    return data


def _proc_read(path):
    with path.open("rb") as source:
        data = source.read(MAX_PROC + 1)
    if len(data) > MAX_PROC:
        raise PreparationError("process observation exceeded size limit")
    return data


def _generation(receipt, proc_root, daemon_uid):
    runtime = receipt.get("runtime")
    if not isinstance(runtime, dict):
        raise PreparationError("host verification lacks daemon generation")
    boot, pid, start = (runtime.get(key) for key in ("boot_id", "pid", "start_ticks"))
    if (
        not isinstance(boot, str)
        or not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", boot)
        or type(pid) is not int
        or pid <= 1
        or type(start) is not int
        or start <= 0
    ):
        raise PreparationError("invalid verified daemon generation")
    proc = Path(proc_root)
    observed_boot = _proc_read(proc / "sys/kernel/random/boot_id").decode().strip()
    process = proc / str(pid)
    raw = _proc_read(process / "stat").decode()
    fields = raw[raw.rindex(")") + 2 :].split()
    if (
        raw.split(" ", 1)[0] != str(pid)
        or fields[0] in {"Z", "X", "x"}
        or int(fields[19]) != start
        or observed_boot != boot
    ):
        raise PreparationError("verified daemon generation is no longer current")
    status = {}
    for line in _proc_read(process / "status").decode().splitlines():
        key, separator, value = line.partition(":")
        if separator:
            if key in status:
                raise PreparationError("duplicate process status field")
            status[key] = value.strip()
    if [int(uid) for uid in status["Uid"].split()] != [daemon_uid] * 4:
        raise PreparationError("verified daemon no longer runs as ollama")
    argv = _proc_read(process / "cmdline")
    if not argv.endswith(b"\0"):
        raise PreparationError("malformed daemon command line")
    words = argv[:-1].decode().split("\0")
    if (
        len(words) != 2
        or Path(words[0]).name != "ollama"
        or words[1] != "serve"
        or (process / "exe").readlink().name != "ollama"
    ):
        raise PreparationError("verified process is not ollama serve")


def _unchanged(path, directory, lock, receipt_fd, raw, owner_uid):
    _trusted(os.fstat(directory), owner_uid, directory=True)
    current = os.stat(path, follow_symlinks=False)
    _trusted(current, owner_uid, directory=True)
    if _identity(current) != _identity(os.fstat(directory)):
        raise PreparationError("preparation directory changed during recovery")
    for name, held in (("preparation.lock", lock), ("verified.json", receipt_fd)):
        _trusted(os.fstat(held), owner_uid)
        with _file(directory, name, owner_uid) as current_fd:
            if _identity(os.fstat(current_fd)) != _identity(os.fstat(held)):
                raise PreparationError("preparation state changed during recovery")
            if name == "verified.json" and _read(current_fd, MAX_RECEIPT) != raw:
                raise PreparationError("verification receipt changed during recovery")


def _recover_attested(config, state, api, *, owner_uid, daemon_uid, proc_root):
    """Internal adapter with filesystem/proc injection for hermetic tests only."""
    validate_config(config)
    try:
        with _directory(state, owner_uid) as (path, directory):
            with _file(directory, "preparation.lock", owner_uid) as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return {
                        "status": "busy",
                        "changed": False,
                        "reason": "preparation-active",
                    }
                try:
                    with _file(directory, "verified.json", owner_uid) as receipt_fd:
                        raw = _read(receipt_fd, MAX_RECEIPT)
                        receipt = json.loads(raw)
                        verified_models(config, receipt)
                        _generation(receipt, proc_root, daemon_uid)
                        _unchanged(path, directory, lock, receipt_fd, raw, owner_uid)
                        result = recover(config, api, receipt)
                        _generation(receipt, proc_root, daemon_uid)
                        _unchanged(path, directory, lock, receipt_fd, raw, owner_uid)
                        return result
                finally:
                    fcntl.flock(lock, fcntl.LOCK_UN)
    except PreparationError:
        raise
    except (OSError, ValueError, TypeError, KeyError, IndexError) as exc:
        raise PreparationError(
            "cannot validate current host preparation receipt or daemon"
        ) from exc


def recover_attested(config, state):
    """Production entry: fixed local Ollama and root-owned read-only authority."""
    try:
        daemon_uid = pwd.getpwnam("ollama").pw_uid
    except KeyError as exc:
        raise PreparationError("ollama account is missing") from exc
    return _recover_attested(
        config,
        state,
        Http("http://127.0.0.1:11434"),
        owner_uid=0,
        daemon_uid=daemon_uid,
        proc_root="/proc",
    )
