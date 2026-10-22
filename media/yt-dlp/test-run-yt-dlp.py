#!/usr/bin/env python3
"""Tests for run-yt-dlp.sh, the wrapper in yt-dlp-config.yaml (BUG-1243).

Offline: a stub yt-dlp records its arguments, prints canned output and exits
with a canned status. The suite runs under the host's sh, then again inside the
yt-dlp image the CronJobs use, under busybox sh with a 64k tmpfs for the
full-volume cases, when docker has that image's linux/amd64 variant locally (it
never pulls). busybox differs from macOS sh where it matters: `grep -f` on an
empty file matches nothing there. The manifest checks need pyyaml, so run it
through uv on the host; the image pass skips them.

  uv run --no-project --with pyyaml python3 media/yt-dlp/test-run-yt-dlp.py
  ... test-run-yt-dlp.py --host-only
  YTDLP_CONFIG=<old yt-dlp-config.yaml> uv run ...    # test another wrapper
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_scripts" / "lib"))
from git_env import scrub_environ  # noqa: E402

scrub_environ()

import copy
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import unittest

try:
    import yaml
except ImportError:
    yaml = None
try:
    import yt_dlp
except ImportError:
    yt_dlp = None

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.environ.get("YTDLP_CONFIG") or os.path.join(HERE, "yt-dlp-config.yaml")
INNER = os.environ.get("YTDLP_TEST_INNER") == "1"
A, B, C = "AAAAAAAAAAA", "BBBBBBBBBBB", "CCCCCCCCCCC"
CAP = "bv*[height<=1080]+ba/b[height<=1080]"
EXIT_3 = {"containerName": "yt-dlp", "operator": "In", "values": [3]}
# The errored lists live beside the archives on the media claim, so a Job
# without that mount would start a fresh list every night and never fail.
VOLUMES = [
    {"name": "config", "configMap": {"name": "yt-dlp-config"}},
    {"name": "media", "persistentVolumeClaim": {"claimName": "media"}},
]
MOUNTS = [
    {"name": "config", "mountPath": "/config"},
    {"name": "media", "mountPath": "/downloads"},
    {"name": "media", "mountPath": "/yt-dlp-archive"},
]


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
            "yt-dlp",
            f'#!/bin/sh\nprintf "%s\\n" "$@" > "{self.d}/args"\n'
            f'cat "{self.d}/out"\nexit "$(cat "{self.d}/rc")"\n',
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
        # Values with spaces and glob characters catch a wrapper that splits "$@".
        return [
            "--match-filters",
            "duration < 10860",
            "--download-archive",
            archive or self.archive,
            "--output",
            "%(title)s *.%(ext)s",
            "URL",
        ]

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
        args = self.args() if args is None else args
        p = self.popen(out, rc, args, limit)
        stdout, _ = p.communicate()
        self.check_forwarded(args)
        return p.returncode, stdout

    def check_forwarded(self, args):
        """yt-dlp got exactly the wrapper's arguments, in order."""
        got = lines(os.path.join(self.d, "args"))
        if got != list(args):
            raise AssertionError(f"yt-dlp got {got}, want {list(args)}")
        os.remove(os.path.join(self.d, "args"))

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
            c1.check_forwarded(c1.args(archive))
            c2.check_forwarded(c2.args(archive))
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


@unittest.skipIf(INNER, "the image has no pyyaml; the host pass checks manifests")
class TestManifests(unittest.TestCase):
    """The ConfigMap and Job templates the wrapper depends on, parsed as YAML."""

    def setUp(self):
        if yaml is None:
            self.fail("needs pyyaml: uv run --no-project --with pyyaml python3 ...")

    def test_the_extracted_blocks_match_the_parsed_configmap(self):
        data = yaml.safe_load(CONFIG_TEXT)["data"]
        for key in ("run-yt-dlp.sh", "yt-dlp.conf"):
            with self.subTest(key=key):
                self.assertEqual(block(CONFIG_TEXT, key), data[key])

    def test_the_config_sets_the_format_once_and_keeps_newline_progress(self):
        conf = yaml.safe_load(CONFIG_TEXT)["data"]["yt-dlp.conf"]
        opts = [shlex.split(x) for x in conf.splitlines() if x.strip() and x[0] != "#"]
        formats = [
            o
            for o in opts
            if o[0] in ("-f", "--format") or o[0].startswith("--format=")
        ]
        self.assertEqual(formats, [["--format", CAP]])
        self.assertIn(["--newline"], opts)

    def jobs(self):
        jobs = []
        for name in ("cronjobs.yaml", "cronjob-validation.yaml"):
            with open(os.path.join(HERE, name)) as f:
                jobs += [
                    d for d in yaml.safe_load_all(f) if d and d["kind"] == "CronJob"
                ]
        self.assertEqual(len(jobs), 6)
        return jobs

    # The fields below are pinned to exact values rather than interpreted: a
    # second policy rule, a configMap items list, a read-only or missing
    # mount, or another image can each change what the wrapper sees, so any
    # change fails here until this test is updated on purpose.

    def test_the_jobs_and_what_they_reference_share_the_namespace(self):
        refs = {
            "yt-dlp-config.yaml": ("ConfigMap", "yt-dlp-config"),
            "yt-dlp-playlists.yaml": ("ConfigMap", "yt-dlp-playlists"),
            "media-pvc.yaml": ("PersistentVolumeClaim", "media"),
        }
        for name, want in refs.items():
            with self.subTest(file=name):
                path = (
                    CONFIG if name == "yt-dlp-config.yaml" else os.path.join(HERE, name)
                )
                with open(path) as f:
                    (doc,) = [d for d in yaml.safe_load_all(f) if d]
                meta = doc["metadata"]
                self.assertEqual(
                    (doc["kind"], meta["name"], meta["namespace"]), (*want, "yt-dlp")
                )
        for job in self.jobs():
            with self.subTest(job=job["metadata"]["name"]):
                self.assertEqual(job["metadata"]["namespace"], "yt-dlp")

    def test_every_job_template_matches_the_validation_job(self):
        jobs = self.jobs()
        self.assertEqual(
            sorted(j["metadata"]["labels"]["app.kubernetes.io/playlist"] for j in jobs),
            list("123456"),
        )
        want = template(jobs[-1])  # cronjob-validation.yaml's
        self.maxDiff = None
        for job in jobs:
            with self.subTest(job=job["metadata"]["name"]):
                self.assertEqual(template(job), want)

    def test_every_job_runs_the_image_the_busybox_pass_tests(self):
        for job in self.jobs():
            with self.subTest(job=job["metadata"]["name"]):
                pod = job["spec"]["jobTemplate"]["spec"]["template"]["spec"]
                self.assertNotIn("initContainers", pod)
                self.assertEqual([c["name"] for c in pod["containers"]], ["yt-dlp"])
                self.assertEqual(pod["containers"][0]["image"], image_tag())

    def test_exit_3_fails_every_job_at_once(self):
        policy = {"rules": [{"action": "FailJob", "onExitCodes": EXIT_3}]}
        for job in self.jobs():
            with self.subTest(job=job["metadata"]["name"]):
                spec = job["spec"]["jobTemplate"]["spec"]
                self.assertEqual(spec["template"]["spec"]["restartPolicy"], "Never")
                self.assertEqual(spec["podFailurePolicy"], policy)

    def test_every_job_mounts_the_configmap_and_the_media_claim(self):
        for job in self.jobs():
            with self.subTest(job=job["metadata"]["name"]):
                pod = job["spec"]["jobTemplate"]["spec"]["template"]["spec"]
                (ctr,) = [c for c in pod["containers"] if c["name"] == "yt-dlp"]
                self.assertEqual(pod["volumes"], VOLUMES)
                self.assertEqual(ctr["volumeMounts"], MOUNTS)

    def test_every_job_script_execs_the_wrapper_and_keeps_its_status(self):
        with open(os.path.join(HERE, "yt-dlp-playlists.yaml")) as f:
            playlists = yaml.safe_load(f)["data"]
        for job in self.jobs():
            for rc in (0, 1, 3):
                with self.subTest(job=job["metadata"]["name"], wrapper_exit=rc):
                    self.run_job_script(job, playlists, rc)

    def run_job_script(self, job, playlists, rc):
        """Run the container's script with its paths moved into a temp dir."""
        t = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, t, True)
        for d in ("config", "downloads", "archive"):
            os.mkdir(os.path.join(t, d))
        with open(os.path.join(t, "config", "run-yt-dlp.sh"), "w") as f:
            f.write(
                f'pwd > "{t}/called.pwd"\nprintf "%s\\n" "$@" > "{t}/called.args"\n'
                f"exit {rc}\n"
            )
        pod = job["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        (ctr,) = [c for c in pod["containers"] if c["name"] == "yt-dlp"]
        self.assertEqual(ctr["command"][:2], ["/bin/sh", "-c"])
        script = (
            ctr["command"][2]
            .replace("/config/", f"{t}/config/")
            .replace("/downloads", f"{t}/downloads")
            .replace("/yt-dlp-archive", f"{t}/archive")
        )
        env = {"PATH": os.environ["PATH"]}
        for e in ctr["env"]:
            ref = e["valueFrom"]["configMapKeyRef"]
            self.assertEqual(ref["name"], "yt-dlp-playlists")
            if ref["key"] in playlists:
                env[e["name"]] = playlists[ref["key"]]
            else:
                self.assertTrue(ref.get("optional"), ref["key"])
        p = subprocess.run(
            ["sh", "-c", script], env=env, capture_output=True, text=True, check=False
        )
        self.assertEqual(p.returncode, rc, p.stdout + p.stderr)
        self.assertTrue(os.path.exists(f"{t}/called.args"), "the wrapper never ran")
        n = job["metadata"]["labels"]["app.kubernetes.io/playlist"]
        want = shlex.split(env.get("EXTRA_ARGS_DEFAULT", "")) + [
            "--config-locations",
            f"{t}/config/yt-dlp.conf",
            "--download-archive",
            f"{t}/archive/archive-playlist-{n}.txt",
            env["PLAYLIST_URL"],
        ]
        self.assertEqual(lines(f"{t}/called.args"), want)
        self.assertEqual(
            lines(f"{t}/called.pwd"), [f"{t}/downloads/{env['PLAYLIST_NAME']}"]
        )


@unittest.skipIf(yt_dlp is None, "yt-dlp is only installed in the image pass")
class TestEffectiveConfig(unittest.TestCase):
    """What yt-dlp itself makes of yt-dlp.conf, in the image."""

    def test_the_config_caps_the_format_and_prints_progress_on_new_lines(self):
        t = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, t, True)
        conf = os.path.join(t, "yt-dlp.conf")
        with open(conf, "w") as f:
            f.write(block(CONFIG_TEXT, "yt-dlp.conf"))
        opts = yt_dlp.parse_options(
            ["--ignore-config", "--config-locations", conf, "URL"]
        )
        self.assertEqual(opts.ydl_opts["format"], CAP)
        self.assertTrue(opts.ydl_opts["progress_with_newline"])


def template(job):
    """The CronJob with its playlist number, schedule and suspend taken out.

    The script is left out too; run_job_script runs each one.
    """
    n = job["metadata"]["labels"]["app.kubernetes.io/playlist"]
    job = copy.deepcopy(job)
    assert job["metadata"]["name"] == f"yt-dlp-playlist-{n}", job["metadata"]["name"]
    job["metadata"]["name"] = "yt-dlp-playlist-N"
    pod = job["spec"]["jobTemplate"]["spec"]["template"]
    for meta in (job["metadata"], pod["metadata"]):
        meta["labels"]["app.kubernetes.io/playlist"] = "N"
    del job["spec"]["schedule"], job["spec"]["suspend"]
    for ctr in pod["spec"]["containers"]:
        ctr["command"][2:3] = ["<script>"]
        for e in ctr.get("env", []):
            ref = e.get("valueFrom", {}).get("configMapKeyRef", {})
            key = ref.get("key", "")
            if key == f"playlist-{n}" or key.startswith(f"playlist-{n}-"):
                ref["key"] = "playlist-N" + key[len(f"playlist-{n}") :]
    return job


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
        ["docker", "image", "inspect", "-f", "{{.Os}}/{{.Architecture}}", image],
        check=False,
        capture_output=True,
        text=True,
    )
    if have.returncode != 0 or have.stdout.strip() != "linux/amd64":
        print(
            f"WARNING: {image} (linux/amd64) is not pulled locally, "
            "so the busybox pass did not run"
        )
        return 0
    print(f"--- busybox pass in {image}", flush=True)
    # The repo's layout under /src, so the scrub header's parents[2] finds
    # _scripts/lib there too (IMPR-1419).
    lib = os.path.join(HERE, "..", "..", "_scripts", "lib")
    return subprocess.run([
        "docker", "run", "--rm", "--pull=never", "--platform", "linux/amd64",
        "--tmpfs", "/full:size=64k", "-e", "FULLFS=/full", "-e", "YTDLP_TEST_INNER=1",
        "-e", "YTDLP_CONFIG=/cfg/yt-dlp-config.yaml",
        "-v", f"{HERE}:/src/media/yt-dlp:ro", "-v", f"{os.path.abspath(lib)}:/src/_scripts/lib:ro",
        "-v", f"{os.path.abspath(CONFIG)}:/cfg/yt-dlp-config.yaml:ro",
        "--entrypoint", "python3", image, "/src/media/yt-dlp/test-run-yt-dlp.py",
    ], check=False).returncode  # fmt: skip


if __name__ == "__main__":
    host_only = "--host-only" in sys.argv
    argv = [a for a in sys.argv if a != "--host-only"]
    result = unittest.main(argv=argv, exit=False).result
    rc = 0 if result.wasSuccessful() else 1
    if not INNER and not host_only:
        rc = image_pass() or rc
    sys.exit(rc)
