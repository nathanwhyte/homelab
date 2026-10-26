"""Hermetic libsystemd ABI, bounded reads and ownership tests; no host journal."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "_scripts" / "lib"))
from git_env import scrub_environ  # noqa: E402

scrub_environ()

import ctypes as C
import errno
import os
import signal
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ollama_journal as journal  # noqa: E402


BOOT = "11111111-2222-3333-4444-555555555555"
INVOCATION = "a" * 32


class Library:
    def __init__(self):
        self.closed = []
        self.matches = []
        self.index = -1
        self.seek = None
        self.flags = None
        self.threshold = None
        self.failure = None
        self.on_open = None
        self.on_next = None
        self.rows = [
            {
                "_BOOT_ID": BOOT.replace("-", ""),
                "_SYSTEMD_INVOCATION_ID": INVOCATION,
                "_PID": "100",
                "MESSAGE": "startup",
                "stamp": 20_000_000,
            }
        ]

    def sd_journal_open(self, target, flags):
        target._obj.value = 101
        self.flags = flags
        if self.on_open:
            self.on_open()
        return 0

    def sd_journal_close(self, handle):
        self.closed.append(handle.value)

    def sd_journal_add_match(self, handle, value, size):
        self.matches.append(value[:size])
        return -errno.EIO if self.failure == "match" else 0

    def sd_journal_set_data_threshold(self, handle, size):
        self.threshold = size
        return 0

    def sd_journal_seek_monotonic_usec(self, handle, boot, since):
        self.seek = (bytes(boot.bytes), since)
        return 0

    def sd_journal_next(self, handle):
        if self.on_next:
            self.on_next()
        self.index += 1
        return int(self.index < len(self.rows))

    def sd_journal_get_monotonic_usec(self, handle, stamp, boot):
        row = self.rows[self.index]
        stamp._obj.value = row["stamp"]
        boot._obj.bytes[:] = bytes.fromhex(row["_BOOT_ID"])
        return 0

    def sd_journal_get_data(self, handle, field, pointer, size):
        if self.failure == "data":
            return -errno.ENOENT
        name = field.decode()
        raw = self.rows[self.index][name]
        if isinstance(raw, str):
            raw = raw.encode()
        self.buffer = C.create_string_buffer(field + b"=" + raw)
        pointer._obj.value = C.addressof(self.buffer)
        size._obj.value = len(field) + 1 + len(raw)
        return 0


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.lib = Library()

    def stream(self, **kwargs):
        return journal.startup_rows(
            BOOT, INVOCATION, 100, 19_000_000, library=self.lib, **kwargs
        )

    def read(self, **kwargs):
        with self.stream(**kwargs) as rows:
            return list(rows)

    def test_exact_index_filters_fields_and_close(self):
        self.assertEqual(self.read()[0]["MESSAGE"], "startup")
        self.assertEqual(self.lib.flags, 5)
        self.assertEqual(
            self.lib.matches,
            [
                b"_BOOT_ID=" + BOOT.replace("-", "").encode(),
                b"_SYSTEMD_INVOCATION_ID=" + INVOCATION.encode(),
                b"_PID=100",
            ],
        )
        self.assertEqual(
            self.lib.seek, (bytes.fromhex(BOOT.replace("-", "")), 19_000_000)
        )
        self.assertEqual(self.lib.closed, [101])
        self.assertEqual(self.lib.threshold, journal.MAX_FIELD + 1)

    def test_unused_metadata_is_never_read(self):
        self.lib.rows[0]["UNUSED_METADATA"] = "x" * (journal.MAX_BYTES + 1)
        self.read()

    def test_early_completion_closes_without_reading_later_rows(self):
        self.lib.rows *= 20001
        with self.stream() as rows:
            next(rows)
        self.assertEqual(self.lib.index, 0)
        self.assertEqual(self.lib.closed, [101])

    def test_empty_retained_journal(self):
        self.lib.rows = []
        self.assertEqual(self.read(), [])
        self.assertEqual(self.lib.closed, [101])

    def test_native_errors_close(self):
        for failure in ("match", "data"):
            with self.subTest(failure=failure):
                self.lib = Library()
                self.lib.failure = failure
                with self.assertRaisesRegex(journal.JournalError, "errno"):
                    self.read()
                self.assertEqual(self.lib.closed, [101])

    def test_threshold_and_oversize_fields_fail_closed(self):
        for size in (journal.MAX_FIELD + 1, journal.MAX_FIELD * 2):
            self.lib = Library()
            self.lib.rows[0]["MESSAGE"] = "x" * size
            with self.assertRaisesRegex(journal.JournalError, "field exceeded"):
                self.read()
            self.assertEqual(self.lib.closed, [101])

    def test_non_utf8_evidence_is_rejected(self):
        self.lib.rows[0]["MESSAGE"] = b"\xff"
        with self.assertRaisesRegex(journal.JournalError, "UTF-8"):
            self.read()

    def test_generation_mismatch_is_rejected(self):
        for key, value in (
            ("_BOOT_ID", "f" * 32),
            ("_PID", "99"),
            ("_SYSTEMD_INVOCATION_ID", "f" * 32),
        ):
            self.lib = Library()
            self.lib.rows[0][key] = value
            with self.assertRaisesRegex(journal.JournalError, "identity mismatch"):
                self.read()

    def test_monotonic_order_and_seek_lower_bound(self):
        for stamps in ([18_000_000], [20_000_000, 19_999_999]):
            self.lib = Library()
            self.lib.rows = [{**self.lib.rows[0], "stamp": value} for value in stamps]
            with self.assertRaisesRegex(journal.JournalError, "monotonic identity"):
                self.read()

    def test_row_and_byte_limits_close(self):
        for setting, value, message in (
            ("MAX_ROWS", 1, "row limit"),
            ("MAX_BYTES", 1, "byte limit"),
        ):
            self.lib = Library()
            self.lib.rows *= 2
            with (
                patch.object(journal, setting, value),
                self.assertRaisesRegex(journal.JournalError, message),
            ):
                self.read()
            self.assertEqual(self.lib.closed, [101])

    def test_deadline_checked_after_native_read(self):
        now = [0]
        self.lib.on_next = lambda: now.__setitem__(0, 21)
        with self.assertRaisesRegex(journal.JournalError, "time limit"):
            self.read(clock=lambda: now[0])
        self.assertEqual(self.lib.closed, [101])

    def test_cancellation_after_acquisition_and_during_read_closes(self):
        def cancelled(signum, frame):
            raise KeyboardInterrupt()

        for signum in (signal.SIGTERM, signal.SIGINT):
            for point in ("on_open", "on_next"):
                self.lib = Library()
                previous = signal.signal(signum, cancelled)
                mask = signal.pthread_sigmask(signal.SIG_BLOCK, set())
                try:
                    setattr(self.lib, point, lambda: os.kill(os.getpid(), signum))
                    with self.assertRaises(KeyboardInterrupt):
                        self.read()
                    self.assertEqual(self.lib.closed, [101])
                    self.assertEqual(
                        signal.pthread_sigmask(signal.SIG_BLOCK, set()), mask
                    )
                finally:
                    signal.signal(signum, previous)

    def test_missing_library_fails_closed(self):
        with patch.object(C, "CDLL", side_effect=OSError("missing")):
            with self.assertRaisesRegex(journal.JournalError, "libsystemd.so.0"):
                journal._library()

    def test_id_abi_layout(self):
        self.assertEqual(C.sizeof(journal._Id128), 16)
        self.assertEqual(C.alignment(journal._Id128), C.alignment(C.c_uint64))

    @unittest.skipUnless(
        shutil.which("cc"), "C compiler unavailable for native ABI fixture"
    )
    def test_id_by_value_matches_compiled_c_union(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "journal-abi.c"
            library = Path(tmp) / "journal-abi.so"
            source.write_text("""#include <stdint.h>
#include <stddef.h>
typedef union { uint8_t bytes[16]; uint64_t qwords[2]; } sd_id128_t;
int check(void *handle, sd_id128_t boot, uint64_t usec) {
    if (handle != (void *)101 || usec != 0x123456789abcdef0ULL) return 0;
    for (int i = 0; i < 16; i++) if (boot.bytes[i] != i + 1) return 0;
    return sizeof(boot) == 16 && _Alignof(sd_id128_t) == 8;
}
""")
            subprocess.run(
                ["cc", "-shared", "-fPIC", "-o", str(library), str(source)],
                check=True,
                capture_output=True,
                timeout=30,
            )
            lib = C.CDLL(str(library))
            lib.check.argtypes = [C.c_void_p, journal._Id128, C.c_uint64]
            lib.check.restype = C.c_int
            self.assertEqual(
                lib.check(
                    101,
                    journal._Id128.from_buffer_copy(bytes(range(1, 17))),
                    0x123456789ABCDEF0,
                ),
                1,
            )


if __name__ == "__main__":
    unittest.main()
