#!/usr/bin/env python3
"""Tests for run-yt-dlp.sh, the wrapper in yt-dlp-config.yaml (BUG-1243).

Stdlib only and offline: a stub yt-dlp prints canned output and exits with a
canned status. The suite runs under the host's sh, then again inside the yt-dlp
image the CronJobs use, under busybox sh with a 64k tmpfs for the full-volume
cases, when docker has that image locally (it never pulls). busybox differs from
macOS sh where it matters: `grep -f` on an empty file matches nothing there.

  python3 media/yt-dlp/test-run-yt-dlp.py              # host, then image
  python3 media/yt-dlp/test-run-yt-dlp.py --host-only
  YTDLP_CONFIG=<old yt-dlp-config.yaml> python3 ...    # test another wrapper
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.environ.get("YTDLP_CONFIG") or os.path.join(HERE, "yt-dlp-config.yaml")
INNER = os.environ.get("YTDLP_TEST_INNER") == "1"
A, B, C = "AAAAAAAAAAA", "BBBBBBBBBBB", "CCCCCCCCCCC"


def block(text, key):
    """The body of the ConfigMap's `key: |` block scalar."""
    lines = text.splitlines()
    start = lines.index(f"  {key}: |") + 1
    body = []
    for line in lines[start:]:
        if line.strip() and not line.startswith("    "):
            break
        body.append(line[4:])
    return "\n".join(body).rstrip("\n") + "\n"


with open(CONFIG) as f:
    CONFIG_TEXT = f.read()
WRAPPER = block(CONFIG_TEXT, "run-yt-dlp.sh")


def err(video_id):
    return f"ERROR: [youtube] {video_id}: Video unavailable. This content isn't available.\n"


def lines(path):
    with open(path) as f:
        return f.read().splitlines()


class Case:
    """A playlist dir: stub yt-dlp, the wrapper with its own log paths, an archive."""

    def __init__(self, root):
        self.d = tempfile.mkdtemp(dir=root)
        self.bin = os.path.join(self.d, "bin")
        os.mkdir(self.bin)
        self.write_exec(
            "yt-dlp", f'#!/bin/sh\ncat "{self.d}/out"\nexit "$(cat "{self.d}/rc")"\n'
        )
        self.run_sh = os.path.join(self.d, "run.sh")
        with open(self.run_sh, "w") as f:
            f.write(WRAPPER.replace("/tmp/yt-dlp.", f"{self.d}/yt-dlp."))
        self.archive = os.path.join(self.d, "archive-playlist-1.txt")
        self.list = os.path.join(self.d, "archive-playlist-1.errored.txt")

    def write_exec(self, name, body):
        path = os.path.join(self.bin, name)
        with open(path, "w") as f:
            f.write(body)
        os.chmod(path, 0o755)

    def args(self, archive=None):
        return ["--download-archive", archive or self.archive, "URL"]

    def popen(self, out, rc, args, limit=False):
        with open(os.path.join(self.d, "out"), "w") as f:
            f.write(out)
        with open(os.path.join(self.d, "rc"), "w") as f:
            f.write(f"{rc}\n")
        cmd = ["sh", self.run_sh, *args]
        if limit:  # a file-size limit that cuts writes short instead of killing
            cmd = ["sh", "-c", 'trap "" XFSZ; ulimit -f 2; exec sh "$@"', "_", *cmd[1:]]
        env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}")
        return subprocess.Popen(
            cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )

    def run(self, out, rc, args=None, limit=False):
        p = self.popen(out, rc, self.args() if args is None else args, limit)
        stdout, _ = p.communicate()
        return p.returncode, stdout

    def leftovers(self, directory=None):
        return [
            n
            for n in os.listdir(directory or self.d)
            if re.search(r"\.errored\.txt\.(tmp|pending)\.", n)
        ]


class WrapperTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.c = Case(self.root)

    def assertExit(self, want, result):
        code, stdout = result
        self.assertEqual(code, want, stdout)


class TestClassification(WrapperTest):
    def test_a_starting_run_records_its_errors_and_passes(self):
        self.assertExit(0, self.c.run(err(A) + err(B), 1))
        self.assertEqual(lines(self.c.list), [A, B])

    def test_known_ids_pass(self):
        self.c.run(err(A) + err(B), 1)
        self.assertExit(0, self.c.run(err(A) + err(B), 1))

    def test_a_new_id_exits_3_and_is_listed(self):
        self.c.run(err(A), 1)
        self.assertExit(3, self.c.run(err(A) + err(C), 1))
        self.assertIn(C, lines(self.c.list))

    def test_a_retry_after_a_new_id_passes(self):
        # Why every Job template turns exit 3 into FailJob (TestManifests).
        self.c.run(err(A), 1)
        self.c.run(err(C), 1)
        self.assertExit(0, self.c.run(err(C), 1))

    def test_an_error_with_no_id_fails_even_on_exit_0(self):
        self.c.run("", 0)
        out = "ERROR: unable to download video data: HTTP Error 403: Forbidden\n"
        self.assertExit(1, self.c.run(out, 0))

    def test_an_error_with_no_id_keeps_the_yt_dlp_status(self):
        self.c.run(err(A), 1)
        out = (
            err(A) + "ERROR: unable to download video data: HTTP Error 403: Forbidden\n"
        )
        self.assertExit(1, self.c.run(out, 1))

    def test_no_id_plus_a_new_id_exits_3(self):
        self.c.run(err(A), 1)
        out = "ERROR: [youtube:tab] PLx: playlist gone\n" + err(B)
        self.assertExit(3, self.c.run(out, 1))

    def test_exit_statuses_without_errors(self):
        self.c.run("", 0)
        for rc, want in ((0, 0), (101, 0), (1, 1), (2, 2), (137, 137)):
            with self.subTest(rc=rc):
                self.assertExit(want, self.c.run("", rc))

    def test_an_empty_starting_list_still_catches_a_new_id(self):
        self.assertExit(0, self.c.run("", 0))
        self.assertEqual(os.path.getsize(self.c.list), 0)
        self.assertExit(3, self.c.run(err(A), 1))

    def test_the_equals_form_of_download_archive(self):
        args = [f"--download-archive={self.c.archive}", "URL"]
        self.assertExit(0, self.c.run(err(A), 1, args))
        self.assertExit(3, self.c.run(err(B), 1, args))

    def test_no_download_archive_counts_every_error_as_new(self):
        self.assertExit(3, self.c.run(err(A), 1, ["URL"]))


class TestListState(WrapperTest):
    def test_a_lost_starting_race_keeps_both_sides(self):
        # Another Job publishes the list between the -e check and ln.
        winner = os.path.join(self.c.d, "winner")
        self.c.write_exec(
            "ln",
            f'#!/bin/sh\nprintf "%s\\n" {C} > "{winner}"\n'
            f'command -p ln "{winner}" "$2"\nexec command -p ln "$@"\n',
        )
        self.assertExit(0, self.c.run(err(A) + err(B), 1))
        self.assertEqual(sorted(lines(self.c.list)), [A, B, C])
        self.assertEqual(self.c.leftovers(), [])

    def test_concurrent_starting_runs_never_drop_an_id(self):
        for _ in range(20):
            c1 = Case(self.root)
            c2 = Case(self.root)
            archive = os.path.join(self.root, f"{os.path.basename(c1.d)}.txt")
            ps = [
                c1.popen(err(A), 1, c1.args(archive)),
                c2.popen(err(B), 1, c2.args(archive)),
            ]
            for p in ps:
                p.communicate()
            listed = lines(archive.replace(".txt", ".errored.txt"))
            self.assertIn(A, listed)
            self.assertIn(B, listed)

    def test_a_list_that_is_not_a_file_fails_with_3(self):
        os.mkdir(self.c.list)
        self.assertExit(3, self.c.run(err(A), 1))

    @unittest.skipIf(os.geteuid() == 0, "root reads a write-only file")
    def test_an_unreadable_list_counts_every_id_as_new(self):
        with open(self.c.list, "w") as f:
            f.write(f"{A}\n")
        os.chmod(self.c.list, 0o200)
        self.addCleanup(os.chmod, self.c.list, 0o600)
        self.assertExit(3, self.c.run(err(A), 1))

    def test_a_missing_list_directory_fails_with_3(self):
        archive = os.path.join(self.c.d, "gone", "archive-playlist-1.txt")
        self.assertExit(3, self.c.run(err(A), 1, self.c.args(archive)))

    def test_a_cut_short_append_is_finished_by_the_next_run(self):
        probe = os.path.join(self.c.d, "probe")
        subprocess.run(
            [
                "sh",
                "-c",
                'trap "" XFSZ; ulimit -f 2; head -c 100000 /dev/zero > "$1"',
                "_",
                probe,
            ],
            check=False,
            stderr=subprocess.DEVNULL,
        )
        limit = os.path.getsize(probe)
        # The append of B and C stops 6 bytes into C.
        with open(self.c.list, "w") as f:
            f.write(f"{A}\n" + "x" * (limit - 12 - 18 - 1) + "\n")
        self.assertExit(3, self.c.run(err(A) + err(B) + err(C), 1, limit=True))
        with open(self.c.list) as f:
            self.assertTrue(f.read().endswith(f"{B}\nCCCCCC"))
        self.assertTrue(any(".pending." in n for n in self.c.leftovers()))
        code, stdout = self.c.run("", 0)
        self.assertEqual(code, 0, stdout)
        self.assertIn("finished an append", stdout)
        self.assertIn(C, lines(self.c.list))
        self.assertExit(0, self.c.run(err(A) + err(B) + err(C), 1))
        self.assertEqual(self.c.leftovers(), [])

    @unittest.skipUnless(
        os.environ.get("FULLFS"), "needs FULLFS, a small tmpfs (image pass)"
    )
    def test_a_full_volume_fails_with_3_and_recovers(self):
        full = os.environ["FULLFS"]
        seed, appended = os.path.join(full, "s"), os.path.join(full, "a")
        for d in (seed, appended):
            shutil.rmtree(d, True)
            os.mkdir(d)
        with open(os.path.join(appended, "archive-playlist-1.errored.txt"), "w") as f:
            f.write(f"{A}\n")
        fill = os.path.join(full, "fill")
        self.addCleanup(lambda: os.path.exists(fill) and os.remove(fill))
        try:
            with open(fill, "wb") as f:
                while True:
                    f.write(b"\0" * 1024)
                    f.flush()
        except OSError:
            pass
        out = err(A) + err(B)
        seed_args = self.c.args(os.path.join(seed, "archive-playlist-1.txt"))
        self.assertExit(3, self.c.run(out, 1, seed_args))
        self.assertEqual(os.listdir(seed), [])
        self.assertExit(
            3,
            self.c.run(
                out, 1, self.c.args(os.path.join(appended, "archive-playlist-1.txt"))
            ),
        )
        os.remove(fill)
        self.assertExit(0, self.c.run(out, 1, seed_args))
        self.assertExit(0, self.c.run(out, 1, seed_args))


class TestManifests(unittest.TestCase):
    """The ConfigMap and Job templates the wrapper depends on."""

    def test_the_config_keeps_newline_progress_and_the_1080p_cap(self):
        conf = block(CONFIG_TEXT, "yt-dlp.conf").splitlines()
        self.assertIn("--newline", conf)
        self.assertIn('--format "bv*[height<=1080]+ba/b[height<=1080]"', conf)

    def test_every_job_runs_the_wrapper_and_fails_at_once_on_exit_3(self):
        text = ""
        for name in ("cronjobs.yaml", "cronjob-validation.yaml"):
            with open(os.path.join(HERE, name)) as f:
                text += f.read()
        jobs = len(re.findall(r"^kind: CronJob$", text, re.MULTILINE))
        self.assertEqual(jobs, 6)
        execs = re.findall(
            r'^\s+exec sh /config/run-yt-dlp\.sh "\$@"$', text, re.MULTILINE
        )
        self.assertEqual(len(execs), jobs)
        policy = re.compile(
            r"podFailurePolicy:\n\s+rules:\n\s+- action: FailJob\n\s+onExitCodes:\n"
            r"\s+containerName: yt-dlp\n\s+operator: In\n\s+values: \[3\]\n"
        )
        self.assertEqual(len(policy.findall(text)), jobs)


def image_tag():
    with open(os.path.join(HERE, "cronjob-validation.yaml")) as f:
        found = re.search(
            r"^\s+image: (nathanwhyte/yt-dlp:\S+)$", f.read(), re.MULTILINE
        )
    return found.group(1)


def image_pass():
    """Rerun this file inside the CronJobs' image, under busybox sh."""
    image = image_tag()
    if not shutil.which("docker"):
        print(f"WARNING: no docker, so the busybox pass in {image} did not run")
        return 0
    have = subprocess.run(
        ["docker", "image", "inspect", image], check=False, capture_output=True
    )
    if have.returncode != 0:
        print(
            f"WARNING: {image} is not pulled locally, so the busybox pass did not run"
        )
        return 0
    print(f"--- busybox pass in {image}", flush=True)
    return subprocess.run([
        "docker", "run", "--rm", "--platform", "linux/amd64",
        "--tmpfs", "/full:size=64k", "-e", "FULLFS=/full", "-e", "YTDLP_TEST_INNER=1",
        "-e", "YTDLP_CONFIG=/cfg/yt-dlp-config.yaml",
        "-v", f"{HERE}:/src:ro", "-v", f"{os.path.abspath(CONFIG)}:/cfg/yt-dlp-config.yaml:ro",
        "--entrypoint", "python3", image, "/src/test-run-yt-dlp.py",
    ], check=False).returncode  # fmt: skip


if __name__ == "__main__":
    host_only = "--host-only" in sys.argv
    argv = [a for a in sys.argv if a != "--host-only"]
    result = unittest.main(argv=argv, exit=False).result
    rc = 0 if result.wasSuccessful() else 1
    if not INNER and not host_only:
        rc = image_pass() or rc
    sys.exit(rc)
