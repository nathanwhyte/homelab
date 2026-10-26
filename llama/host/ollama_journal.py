"""Read a bounded startup stream using journald's boot/monotonic index.

ABI: systemd v257 sd-journal.h and sd-id128.h. These APIs date to v187.
Only requested fields are copied; unrelated metadata never consumes the proof
budget. The caller closes the stream once its generation's startup completes.
"""

import ctypes as C
import time

from ollama_signals import deferred_context, interruptible, owned_descriptor


class JournalError(RuntimeError):
    """Required retained journal evidence could not be read safely."""


class _Id128(C.Structure):
    # sd_id128_t is a union of 16 bytes / two uint64_t words. ctypes does not
    # support unions by value. Use its integer-word ABI representation (same
    # size, alignment and integer argument classes), checked against a compiled
    # C union fixture in the test suite.
    _fields_ = [("qwords", C.c_uint64 * 2)]

    @property
    def bytes(self):
        return (C.c_uint8 * 16).from_buffer(self)


MAX_FIELD = 256 * 1024
MAX_BYTES = 16 * 1024 * 1024
MAX_ROWS = 20000
READ_SECONDS = 20


def _library():
    try:
        lib = C.CDLL("libsystemd.so.0")
        pointer = C.c_void_p
        signatures = {
            "open": [C.POINTER(pointer), C.c_int],
            "close": [pointer],
            "add_match": [pointer, pointer, C.c_size_t],
            "set_data_threshold": [pointer, C.c_size_t],
            "seek_monotonic_usec": [pointer, _Id128, C.c_uint64],
            "next": [pointer],
            "get_monotonic_usec": [pointer, C.POINTER(C.c_uint64), C.POINTER(_Id128)],
            "get_data": [
                pointer,
                C.c_char_p,
                C.POINTER(pointer),
                C.POINTER(C.c_size_t),
            ],
        }
        for name, args in signatures.items():
            function = getattr(lib, "sd_journal_" + name)
            function.argtypes = args
            function.restype = None if name == "close" else C.c_int
        return lib
    except (OSError, AttributeError) as exc:
        raise JournalError(
            "libsystemd.so.0 journal APIs are required on the host"
        ) from exc


def _check(result, operation):
    if result < 0:
        raise JournalError(f"journal {operation} failed (errno {-result})")
    return result


@deferred_context(critical=False)
def startup_rows(boot, invocation, pid, since, *, library=None, clock=time.monotonic):
    """Own the native journal handle even on cancellation or early stream exit."""
    lib = library or _library()
    deadline = clock() + READ_SECONDS

    def call(name, *args):
        if clock() > deadline:
            raise JournalError("journal startup read exceeded time limit")
        result = interruptible(getattr(lib, "sd_journal_" + name), *args)
        if clock() > deadline:
            raise JournalError("journal startup read exceeded time limit")
        return _check(result, name)

    def open_handle():
        handle = C.c_void_p()
        # Acquisition is masked until owned_descriptor has registered cleanup.
        _check(lib.sd_journal_open(C.byref(handle), 5), "open")
        return handle

    with owned_descriptor(open_handle, closer=lib.sd_journal_close) as handle:
        identity = {
            "_BOOT_ID": boot.replace("-", ""),
            "_SYSTEMD_INVOCATION_ID": invocation,
            "_PID": str(pid),
        }
        boot_id = _Id128.from_buffer_copy(bytes.fromhex(identity["_BOOT_ID"]))
        for key, value in identity.items():
            match = f"{key}={value}".encode()
            call("add_match", handle, match, len(match))
        # The threshold is a hint. Reject oversize/truncated fields rather than
        # accepting a prefix; include FIELD= in the budget.
        call("set_data_threshold", handle, MAX_FIELD + 1)
        call("seek_monotonic_usec", handle, boot_id, since)

        def rows():
            total = 0
            count = 0
            previous = since
            while call("next", handle):
                count += 1
                if count > MAX_ROWS:
                    raise JournalError("journal startup exceeded row limit")
                stamp, observed_boot = C.c_uint64(), _Id128()
                call(
                    "get_monotonic_usec", handle, C.byref(stamp), C.byref(observed_boot)
                )
                if (
                    bytes(observed_boot.bytes) != bytes(boot_id.bytes)
                    or stamp.value < previous
                ):
                    raise JournalError("journal boot/monotonic identity mismatch")
                previous = stamp.value
                row = {"__MONOTONIC_TIMESTAMP": str(stamp.value)}
                for field in (*identity, "MESSAGE"):
                    data, size = C.c_void_p(), C.c_size_t()
                    call(
                        "get_data", handle, field.encode(), C.byref(data), C.byref(size)
                    )
                    if size.value > MAX_FIELD:
                        raise JournalError("journal startup field exceeded size limit")
                    total += size.value
                    if total > MAX_BYTES:
                        raise JournalError("journal startup exceeded byte limit")
                    raw = C.string_at(data, size.value)
                    prefix = field.encode() + b"="
                    if not raw.startswith(prefix):
                        raise JournalError("malformed journal field")
                    try:
                        row[field] = raw[len(prefix) :].decode("utf-8")
                    except UnicodeError as exc:
                        raise JournalError("non-UTF-8 journal evidence") from exc
                if any(row[key] != value for key, value in identity.items()):
                    raise JournalError("journal generation identity mismatch")
                yield row

        yield rows()
