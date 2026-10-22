#!/usr/bin/env python3
"""Hermetic tests for maintenance-active.py (IDEA-1150): no cluster, a stub kubectl."""

import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/maintenance-active.py"
spec = importlib.util.spec_from_file_location("maintenance_active", SCRIPT)
ma = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ma)


def matcher(name, value, regex=False, equal=True):
    return {"name": name, "value": value, "isRegex": regex, "isEqual": equal}


MATCH_ALL = matcher("alertname", ".+", regex=True)


def silence(*matchers, state="active", sid="s1"):
    return {
        "id": sid,
        "status": {"state": state},
        "endsAt": "2026-10-23T00:00:00.123456789Z",
        "comment": "planned",
        "matchers": list(matchers),
    }


class SilenceTests(unittest.TestCase):
    def test_timmy_offline_silence_covers_everything(self):
        self.assertEqual(ma.silence_scope(silence(MATCH_ALL)), ma.ALL)
        self.assertEqual(
            ma.silence_scope(silence(matcher("alertname", ".*", regex=True))), ma.ALL
        )

    def test_namespace_equality_narrows_it(self):
        s = silence(MATCH_ALL, matcher("namespace", "viking"))
        self.assertEqual(ma.silence_scope(s), "viking")

    def test_regex_or_negated_namespace_does_not_count(self):
        # Round 1 P2: Python's regex engine is not Alertmanager's (Go RE2).
        for m in (
            matcher("namespace", "[[:alpha:]]+", regex=True, equal=False),
            matcher("namespace", "(?<ns>viking)", regex=True),
            matcher("namespace", "viking", equal=False),
        ):
            self.assertIsNone(ma.silence_scope(silence(MATCH_ALL, m)), m)

    def test_a_namespace_no_pod_can_have_does_not_count(self):
        # Round 4 P3: namespace="*" matches no alert but equalled ALL.
        for value in ("*", "Viking", "", "a" * 64, "-viking"):
            s = silence(MATCH_ALL, matcher("namespace", value))
            self.assertIsNone(ma.silence_scope(s), value)

    def test_match_all_on_an_optional_label_does_not_count(self):
        # Round 1 P2: job=~".+" fails every alert without a job label.
        self.assertIsNone(ma.silence_scope(silence(matcher("job", ".+", regex=True))))
        self.assertIsNone(
            ma.silence_scope(silence(MATCH_ALL, matcher("job", ".+", regex=True)))
        )

    def test_one_alert_name_is_not_maintenance(self):
        s = silence(
            matcher("alertname", "KubeJobFailed"), matcher("namespace", "viking")
        )
        self.assertIsNone(ma.silence_scope(s))

    def test_namespace_alone_or_two_namespaces_do_not_count(self):
        self.assertIsNone(ma.silence_scope(silence(matcher("namespace", "viking"))))
        s = silence(
            MATCH_ALL, matcher("namespace", "viking"), matcher("namespace", "garage")
        )
        self.assertIsNone(ma.silence_scope(s))

    def test_only_active_silences_count(self):
        for state in ("expired", "pending", None):
            s = silence(MATCH_ALL, state=state)
            self.assertIsNone(ma.silence_scope(s))

    def test_end_is_shown_in_local_time(self):
        # Round 4 P3: amtool's endsAt is UTC with up to nanoseconds.
        shown = ma.ends_at("2026-10-23T00:00:00.123456789Z")
        self.assertNotIn("T00:00", shown)
        self.assertRegex(shown, r"^2026-10-2[23] \d\d:\d\d \S+ [+-]\d{4}$")
        self.assertEqual(ma.ends_at("not a time"), "not a time")


class TargetTests(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(ma.parse_target("viking"), (None, "viking", None))
        self.assertEqual(
            ma.parse_target("Deployment/viking/openviking"),
            ("deployment", "viking", "openviking"),
        )
        for bad in ("", "viking/openviking", "a//b", "a/b/c/d"):
            with self.assertRaises(ValueError):
                ma.parse_target(bad)

    def test_covers(self):
        t = ("deployment", "viking", "openviking")
        self.assertTrue(ma.covers(ma.ALL, t))
        self.assertTrue(ma.covers("viking", t))
        self.assertTrue(ma.covers("viking", (None, "viking", None)))
        self.assertFalse(ma.covers("garage", t))


TIMMY = "homelab-timmy-offline/window"
NODE = "homelab-node-maintenance/window"


class LocalStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.now = time.time()

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def windows(self):
        return ma.local_windows(self.root, self.now)

    def test_nothing_recorded(self):
        self.assertEqual(self.windows(), ([], []))

    def test_open_windows_cover_everything(self):
        self.write(TIMMY, f"{int(self.now) + 60} planned timmy outage\n")
        self.write(NODE, f"{int(self.now) + 60} reboot cycle on wemby\n")
        found, unreadable = self.windows()
        self.assertEqual(unreadable, [])
        self.assertEqual([scope for scope, _ in found], [ma.ALL, ma.ALL])
        self.assertTrue(
            found[0][1].startswith("timmy-offline: planned timmy outage until ")
        )
        self.assertTrue(
            found[1][1].startswith("node-maintenance: reboot cycle on wemby until ")
        )

    def test_a_window_past_its_end_does_not_count(self):
        # Round 4 P2s: undo state left by a failed expire or a backed-out cycle
        # outlived the window; the window file's end bounds it.
        self.write(TIMMY, f"{int(self.now)} planned timmy outage\n")
        self.write(NODE, f"{int(self.now) - 3600} reboot cycle on timmy\n")
        self.assertEqual(self.windows(), ([], []))

    def test_undo_state_alone_is_not_a_window(self):
        for rel in (
            "homelab-timmy-offline/silence-id",
            "homelab-timmy-offline/parked-scales",
            "homelab-timmy-offline/suspended-cronjobs",
            "homelab-node-maintenance/timmy.boot-id",
            "homelab-node-maintenance/spin-down-replicas",
            "homelab-node-maintenance/lock/pid",
        ):
            self.write(rel, f"{os.getpid()}\n")
        self.assertEqual(self.windows(), ([], []))

    def test_malformed_window_is_unreadable(self):
        for text in (
            "",
            "\n",
            "soon reboot\n",
            "123\n",
            "123  \n",
            "-5 x\n",
            "١٢٣ x\n",
            # Round 5 P3s: only exactly what the producers' printf writes.
            f"{int(self.now) + 60} x\nmore\n",
            f"{int(self.now) + 60} x",
            f"0{int(self.now) + 60} x\n",
            f"{int(self.now) + 60} \x00\n",
            "253402300799 year 9999\n",
            "9" * 5000 + " forever\n",
        ):
            with self.subTest(text=text[:40]):
                self.write(TIMMY, text)
                found, unreadable = self.windows()
                self.assertEqual(found, [])
                self.assertEqual(len(unreadable), 1)

    def test_a_malformed_window_does_not_hide_its_sibling(self):
        # Round 5 P3: a huge end raised outside the guard and lost this one.
        self.write(TIMMY, "9" * 5000 + " forever\n")
        self.write(NODE, f"{int(self.now) + 60} reboot cycle on wemby\n")
        found, unreadable = self.windows()
        self.assertEqual(len(found), 1)
        self.assertEqual(len(unreadable), 1)

    def test_a_non_regular_window_path_is_unreadable(self):
        # Round 5 P3: a directory there swallowed the producer's mv.
        (self.root / NODE).mkdir(parents=True)
        fifo = self.root / "homelab-timmy-offline/fifo"
        fifo.parent.mkdir()
        os.mkfifo(fifo)
        (self.root / TIMMY).symlink_to(fifo)
        found, unreadable = self.windows()
        self.assertEqual(found, [])
        self.assertEqual(len(unreadable), 2)
        self.assertTrue(all("not a regular file" in u for u in unreadable))

    @unittest.skipIf(os.geteuid() == 0, "root reads mode-000 files")
    def test_unreadable_window_is_reported_not_treated_as_absent(self):
        for rel in (TIMMY, "homelab-timmy-offline"):
            with self.subTest(rel=rel):
                self.write(TIMMY, f"{int(self.now) + 60} outage\n")
                path = self.root / rel
                path.chmod(0)
                try:
                    found, unreadable = self.windows()
                finally:
                    path.chmod(0o700)
                self.assertEqual(found, [])
                self.assertEqual(len(unreadable), 1)
                self.assertIn("homelab-timmy-offline", unreadable[0])


def bash(script, source, env):
    return subprocess.run(
        ["bash", "-c", f'source "$1"; {script}', "bash", str(ROOT / source)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=True,
    )


class ProducerRoundTripTests(unittest.TestCase):
    """The producers' own functions write what the helper reads, at the path
    their own STATE_ROOT default resolves to (round 4 P2: a substring check
    missed a moved state root)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.bin = bin_dir = tmp / "bin"
        bin_dir.mkdir()
        stub = bin_dir / "kubectl"
        stub.write_text(
            '#!/bin/sh\ncase "$*" in\n'
            '  *amtool*) printf "[]" ;;\n'
            '  "get nodes -o json") printf \'{"items":[]}\' ;;\n'
            "  *) exit 99 ;;\nesac\n"
        )
        stub.chmod(0o755)
        self.home = tmp / "home"
        self.home.mkdir()
        self.env = {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "HOME": str(self.home),
            "KUBECTL": str(stub),
        }

    def tearDown(self):
        self.tmp.cleanup()

    def helper(self):
        out = subprocess.run(
            [sys.executable, "-I", str(SCRIPT), "--json", "viking"],
            capture_output=True,
            text=True,
            env=self.env,
            timeout=30,
        )
        return out.returncode, json.loads(out.stdout)

    def test_timmy_offline_window(self):
        bash("open_window", "scripts/timmy-offline.sh", self.env)
        code, out = self.helper()
        self.assertEqual(code, 0, out)
        self.assertTrue(
            out["windows"][0].startswith(
                "timmy-offline: planned timmy outage (timmy-offline.sh down) until "
            )
        )
        bash("close_window", "scripts/timmy-offline.sh", self.env)
        self.assertEqual(self.helper()[0], 1)

    def test_node_maintenance_window(self):
        src = "scripts/node-maintenance.sh"
        bash('open_maintenance_window "reboot cycle on wemby"', src, self.env)
        code, out = self.helper()
        self.assertEqual(code, 0, out)
        self.assertTrue(
            out["windows"][0].startswith(
                "node-maintenance: reboot cycle on wemby until "
            )
        )
        bash("close_maintenance_window", src, self.env)
        self.assertEqual(self.helper()[0], 1)

    def test_window_ends_on_its_own(self):
        env = {**self.env, "MAINTENANCE_WINDOW_SECONDS": "0"}
        bash(
            'open_maintenance_window "reboot cycle on wemby"',
            "scripts/node-maintenance.sh",
            env,
        )
        self.assertEqual(self.helper()[0], 1)

    def test_restore_services_closes_a_stopped_cycles_window(self):
        src = "scripts/node-maintenance.sh"
        bash(
            'open_maintenance_window "reboot cycle on wemby"; cmd_restore_services',
            src,
            self.env,
        )
        self.assertEqual(self.helper()[0], 1)

    def test_xdg_state_home_moves_both_sides(self):
        # Round 5 P3: the round trips above leave XDG_STATE_HOME unset.
        xdg = Path(self.tmp.name) / "xdg"
        self.env["XDG_STATE_HOME"] = str(xdg)
        bash("open_window", "scripts/timmy-offline.sh", self.env)
        bash('open_maintenance_window "x"', "scripts/node-maintenance.sh", self.env)
        self.assertTrue((xdg / TIMMY).is_file())
        self.assertTrue((xdg / NODE).is_file())
        self.assertFalse((self.home / ".local").exists())
        code, out = self.helper()
        self.assertEqual((code, len(out["windows"])), (0, 2))

    def test_restore_services_leaves_an_active_cycles_window(self):
        # It refuses while a cycle owns the state; the window stays with it.
        src = "scripts/node-maintenance.sh"
        with self.assertRaises(subprocess.CalledProcessError):
            bash(
                'open_maintenance_window "reboot cycle on wemby";'
                " save_cycle_boot_id wemby x; cmd_restore_services",
                src,
                self.env,
            )
        self.assertEqual(self.helper()[0], 0)


STUB = """#!/bin/sh
case "$*" in
  *amtool*) [ -n "$STUB_SILENCES_FAIL" ] && { echo "connection refused" >&2; exit 1; }
            printf '%s' "$STUB_SILENCES" ;;
  *) echo "unexpected kubectl call: $*" >&2; exit 99 ;;
esac
"""


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.state = tmp / "state"
        self.state.mkdir()
        kubectl = tmp / "kubectl"
        kubectl.write_text(STUB)
        kubectl.chmod(kubectl.stat().st_mode | stat.S_IXUSR)
        self.env = {
            "PATH": os.environ["PATH"],
            "KUBECTL": str(kubectl),
            "XDG_STATE_HOME": str(self.state),
            "STUB_SILENCES": "[]",
        }

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args, **env):
        return subprocess.run(
            [sys.executable, "-I", str(SCRIPT), *args],
            capture_output=True,
            text=True,
            env={**self.env, **env},
            timeout=30,
        )

    def cli(self, *args, **env):
        out = self.run_cli("--json", *args, **env)
        return out.returncode, json.loads(out.stdout)

    def open_window(self):
        path = self.state / TIMMY
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{int(time.time()) + 600} planned timmy outage\n")
        return path

    def test_no_window_exits_1_without_reading_the_deployment(self):
        # Round 3 P2: a Deployment's Progressing reason is not a window, so the
        # stub's "unexpected kubectl call" never fires (it would be unreadable).
        code, out = self.cli("deployment/viking/openviking")
        self.assertEqual((code, out["windows"], out["unreadable"]), (1, [], []))

    def test_namespace_silence_covers_only_its_namespace(self):
        s = silence(MATCH_ALL, matcher("namespace", "viking"))
        code, out = self.cli("viking", "garage", STUB_SILENCES=json.dumps([s]))
        self.assertEqual((code, out["uncovered"]), (1, ["garage"]))
        code, out = self.cli(
            "deployment/viking/openviking", STUB_SILENCES=json.dumps([s])
        )
        self.assertEqual(code, 0)
        self.assertTrue(out["windows"][0].startswith("silence: s1 on namespace viking"))

    def test_cluster_silence_with_no_targets(self):
        code, out = self.cli(STUB_SILENCES=json.dumps([silence(MATCH_ALL)]))
        self.assertEqual(code, 0)
        self.assertTrue(out["windows"][0].startswith("silence: s1 on all targets"))
        s = silence(MATCH_ALL, matcher("namespace", "viking"))
        code, _ = self.cli(STUB_SILENCES=json.dumps([s]))
        self.assertEqual(code, 1)

    def test_unreadable_alertmanager_without_a_window_exits_2(self):
        code, out = self.cli("viking", STUB_SILENCES_FAIL="1")
        self.assertEqual(code, 2)
        self.assertIn("connection refused", out["unreadable"][0])

    def test_malformed_silence_output_is_unreadable(self):
        # Round 3 P2: every malformed shape, empty output included, is exit 2.
        for raw in (
            '{"not": "a list"}',
            "",
            "[null]",
            '[{"status": null}]',
            '[{"status": {"state": "active"}, "matchers": []}]',
            '[{"status": {"state": "active"}, "matchers": [{"name": "alertname"}]}]',
            '[{"status": {"state": "active"}, "matchers": '
            '[{"name": "alertname", "value": ".+", "isRegex": "yes"}]}]',
        ):
            with self.subTest(raw=raw):
                code, out = self.cli("viking", STUB_SILENCES=raw)
                self.assertEqual(code, 2)
                self.assertEqual(len(out["unreadable"]), 1)

    def test_local_window_survives_malformed_silence_output(self):
        self.open_window()
        code, out = self.cli("viking", STUB_SILENCES='[{"status": null}]')
        self.assertEqual(code, 0)
        self.assertEqual(len(out["unreadable"]), 1)

    def test_local_window_wins_over_an_unreadable_cluster(self):
        self.open_window()
        code, out = self.cli("viking", STUB_SILENCES_FAIL="1")
        self.assertEqual(code, 0)
        self.assertEqual(len(out["unreadable"]), 1)

    @unittest.skipIf(os.geteuid() == 0, "root reads mode-000 directories")
    def test_cli_exits_2_on_an_unreadable_state_directory(self):
        # Round 4 P3: the stub answers "[]", so only the directory is unreadable.
        self.open_window()
        d = self.state / "homelab-timmy-offline"
        d.chmod(0)
        try:
            code, out = self.cli("viking")
        finally:
            d.chmod(0o700)
        self.assertEqual(code, 2)
        self.assertEqual(len(out["unreadable"]), 1)
        self.assertIn("homelab-timmy-offline", out["unreadable"][0])

    def test_text_output_names_the_window(self):
        self.open_window()
        out = self.run_cli("viking")
        self.assertEqual(out.returncode, 0)
        self.assertTrue(
            out.stdout.startswith("timmy-offline: planned timmy outage until ")
        )

    def test_options_may_follow_targets(self):
        code, _ = self.cli("viking", "--json", "garage")
        self.assertEqual(code, 1)

    def test_bad_target_is_a_usage_error(self):
        # Round 4 P3: 2 means "could not tell"; a usage error is 64. Round 5
        # P3: so is --help, whose 0 would mean "covered".
        for args in (["viking/openviking"], ["--bogus", "viking"]):
            with self.subTest(args=args):
                out = self.run_cli("--json", *args)
                self.assertEqual(out.returncode, 64)
                self.assertEqual(out.stdout, "")
        for args in (["-h"], ["viking", "--help"]):
            with self.subTest(args=args):
                out = self.run_cli(*args)
                self.assertEqual(out.returncode, 64)
                self.assertTrue(out.stdout.startswith("usage: "))
        self.assertIn("kind/namespace/name", self.run_cli("viking/openviking").stderr)


if __name__ == "__main__":
    unittest.main()
