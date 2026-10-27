"""Exporter-produced fixtures across the three real caller seams. Offline."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_scripts" / "lib"))
from git_env import scrub_environ  # noqa: E402

scrub_environ()

import importlib.util
import os
import shlex
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import yaml

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(TOOLS.parent / "exporters"))
from lock_observations import LOCK_METRIC, observe_exporter


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


exporter = load("observation_exporter", TOOLS.parent / "exporters/ov-exporter.py")
watcher = load("observation_watcher", TOOLS / "ov-contention-watch.py")
watchdog = load("observation_watchdog", TOOLS / "ov-lock-watchdog.py")
janitor = load("observation_janitor", TOOLS / "ovlock-janitor.py")
HEADER = f"# TYPE {LOCK_METRIC} gauge\n"


def scrape(key="normal", age=3600):
    labels = (
        exporter.lock_labels(key, {"owner": "owner", "lock_type": "T"})
        if key is not None
        else {}
    )
    with patch.dict(
        exporter._metrics,
        {
            "openviking_up": 1,
            "openviking_last_poll_unix": 100,
            "openviking_s3_ovlock_poll_ok": 1,
            LOCK_METRIC: [(labels, age)] if key is not None else [],
        },
    ):
        return exporter.format_metrics()


class ExporterObservations(unittest.TestCase):
    def test_exporter_labels_round_trip_through_every_caller(self):
        for key in [
            "normal",
            "brace}key",
            'quote"key',
            "slash\\key",
            "newline\nkey",
            'mixed\\"}\nkey',
            "default/user/noot/sessions/new\nline/.path.ovlock",
            *(f"unicode{chr(code)}key" for code in [0x85, 0x2028, 0x2029]),
        ]:
            with self.subTest(key=key):
                text = scrape(key)
                observation = observe_exporter(
                    text, source="fixture-pod", observed_at=123
                )
                self.assertEqual(
                    (observation.source, observation.observed_at), ("fixture-pod", 123)
                )
                self.assertEqual(observation.lock_status, "valid")
                self.assertEqual(observation.require_locks()[0].key, key)
                self.assertIn("top N", observation.coverage)
                self.assertEqual(observation.last_poll, 100)
                self.assertEqual(janitor.parse_presence_ages(text), {key: 3600})
                self.assertEqual(watchdog.parse_exporter(text).locks[0].key, key)
                sink = Mock()
                with patch.object(watcher, "run", return_value=text):
                    got = watcher.metrics(sink, "fixture-pod")
                self.assertEqual(got.locks[0].key, key)
                self.assertIn("fixture-pod", got.source)
                sink.emit.assert_not_called()

    def test_empty_selection_is_distinct_from_missing_family(self):
        text = scrape(None)
        empty = observe_exporter(text)
        self.assertEqual((empty.lock_status, empty.locks), ("valid", ()))
        self.assertEqual(janitor.parse_presence_ages(text), {})
        self.assertEqual(watchdog.parse_exporter(text).locks, ())
        missing = observe_exporter(text.replace(HEADER, ""), source="old-exporter")
        self.assertEqual((missing.lock_status, missing.locks), ("missing", None))
        with self.assertRaisesRegex(ValueError, "old-exporter.*missing lock family"):
            missing.require_locks()

    def test_any_malformed_lock_sample_invalidates_the_whole_selection(self):
        rows = [
            f'{LOCK_METRIC}{{key="bad"}} {value}\n'
            for value in ["NaN", "+Inf", "-Inf", "-1", "bad-number"]
        ] + [
            f'{LOCK_METRIC}{{key="bad"\n',
            f'{LOCK_METRIC}{{key=""}} 1\n',
            f'{LOCK_METRIC}{{owner="o"}} 1\n',
            f'{LOCK_METRIC}{{key="bad",junk}} 1\n',
            f'{LOCK_METRIC}{{key="bad",key="other"}} 1\n',
            f'{LOCK_METRIC}{{key="bad\\q"}} 1\n',
            f'{LOCK_METRIC},{{key="bad"}} 1\n',
            f'{LOCK_METRIC}{{key="normal"}} 1\n',
        ]
        for bad in rows:
            for text in [scrape() + bad, bad + scrape()]:
                with self.subTest(bad=bad, first=text.startswith(bad)):
                    observation = observe_exporter(text, source="fixture")
                    self.assertEqual(
                        (observation.lock_status, observation.locks),
                        ("malformed", None),
                    )
                    self.assertTrue(observation.diagnostics[0].line)
                    with self.assertRaises(ValueError):
                        janitor.parse_presence_ages(text)
                    with self.assertRaises(watchdog.ParseError):
                        watchdog.parse_exporter(text)
                    sink = Mock()
                    with patch.object(watcher, "run", return_value=text):
                        self.assertIsNone(watcher.metrics(sink, "fixture-pod"))
                    self.assertIn("WATCH ERROR metrics:", sink.emit.call_args[0][0])
                    sink.ok.assert_not_called()

    def test_all_diagnostics_survive_a_bad_selection(self):
        observation = observe_exporter(
            HEADER
            + f'{LOCK_METRIC}{{key="a"}} NaN\n'
            + f'{LOCK_METRIC}{{key="b"}} Inf\n'
        )
        self.assertEqual([d.line for d in observation.diagnostics], [2, 3])

    def test_invalid_or_duplicate_declaration_is_malformed(self):
        for header in [
            HEADER.replace("gauge", "counter"),
            HEADER + HEADER,
            HEADER.rstrip() + " extra\n",
        ]:
            with self.subTest(header=header):
                observation = observe_exporter(header)
                self.assertEqual(
                    (observation.lock_status, observation.locks), ("malformed", None)
                )

    def test_health_failure_does_not_erase_decoded_evidence(self):
        text = scrape().replace(
            "openviking_s3_ovlock_poll_ok 1", "openviking_s3_ovlock_poll_ok 0"
        )
        observation = observe_exporter(text)
        self.assertFalse(observation.lock_poll_ok)
        self.assertEqual(len(observation.require_locks()), 1)

    def test_scalar_and_queue_errors_remain_separate_from_lock_validity(self):
        for name in [
            "openviking_up",
            "openviking_last_poll_unix",
            "openviking_queue_pending",
            "openviking_queue_in_progress",
        ]:
            text = scrape() + f"{name} NaN\n"
            observation = observe_exporter(text)
            self.assertEqual(len(observation.require_locks()), 1)
            with self.assertRaises(watchdog.ParseError):
                watchdog.parse_exporter(text)

    def test_irrelevant_malformed_metrics_do_not_poison_locks(self):
        self.assertEqual(
            len(observe_exporter(scrape() + "other_metric NaN\n").require_locks()), 1
        )


def configmap_files(path):
    """Read the generated literal blocks without a YAML dependency."""
    files = {}
    current = None
    for line in path.read_text().splitlines():
        if (
            line.startswith("  ")
            and not line.startswith("   ")
            and line.endswith(": |")
        ):
            current = line[2:-3]
            files[current] = []
        elif current is not None and (not line or line.startswith("    ")):
            files[current].append(line[4:])
        else:
            current = None
    return {name: "\n".join(lines) + "\n" for name, lines in files.items()}


class PackagedObservations(unittest.TestCase):
    def test_actual_hook_dispatch_covers_all_packaging_inputs(self):
        root = TOOLS.parents[1]
        config = yaml.safe_load((root / ".pre-commit-config.yaml").read_text())
        hook = next(
            hook
            for repo in config["repos"]
            for hook in repo["hooks"]
            if hook["id"] == "lock-observation-tests"
        )
        # Exercise pre-commit's own matcher with the real hook configuration.
        # Replace only execution/environment: source parity and imports run above.
        hook = dict(
            hook,
            language="system",
            entry=shlex.join([sys.executable, "-c", "print('OBSERVATION_HOOK_RAN')"]),
        )
        hook.pop("additional_dependencies", None)
        dependencies = [
            ".pre-commit-config.yaml",
            "viking/tools/lock_observations.py",
            "viking/tools/test_lock_observations.py",
            "viking/tools/ov-contention-watch.py",
            "viking/tools/ov-lock-watchdog.py",
            "viking/tools/ovlock-janitor.py",
            "viking/exporters/ov-exporter.py",
            "viking/exporters/s3lite.py",
            "viking/scripts/regen-janitor-configmap.sh",
            "viking/scripts/regen-lock-watchdog-configmap.sh",
            "viking/manifests/ovlock-janitor-configmap.yaml",
            "viking/manifests/ov-lock-watchdog-configmap.yaml",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, PRE_COMMIT_HOME=str(Path(tmp, "cache")))
            subprocess.run(["git", "init", "-q", tmp], check=True, env=env)
            dispatch_config = Path(tmp, "dispatch.yaml")
            dispatch_config.write_text(
                yaml.safe_dump({"repos": [{"repo": "local", "hooks": [hook]}]})
            )
            for path in dependencies + ["unrelated.py"]:
                fixture = Path(tmp, path)
                fixture.parent.mkdir(parents=True, exist_ok=True)
                fixture.write_text("fixture\n")
            subprocess.run(
                ["git", "add", "--", *dependencies, "unrelated.py"],
                cwd=tmp,
                check=True,
                env=env,
            )
            for path in dependencies + ["unrelated.py"]:
                with self.subTest(path=path):
                    result = subprocess.run(
                        [
                            "pre-commit",
                            "run",
                            "--config",
                            str(dispatch_config),
                            "lock-observation-tests",
                            "--verbose",
                            "--files",
                            path,
                        ],
                        cwd=tmp,
                        env=env,
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    self.assertEqual(
                        result.returncode, 0, result.stdout + result.stderr
                    )
                    self.assertEqual(
                        "OBSERVATION_HOOK_RAN" in result.stdout,
                        path != "unrelated.py",
                        result.stdout,
                    )

    def test_configmaps_ship_current_sources_and_import_without_repo_paths(self):
        for stem, script in [
            ("ov-lock-watchdog", "ov-lock-watchdog.py"),
            ("ovlock-janitor", "ovlock-janitor.py"),
        ]:
            with self.subTest(configmap=stem), tempfile.TemporaryDirectory() as tmp:
                files = configmap_files(
                    TOOLS.parent / "manifests" / f"{stem}-configmap.yaml"
                )
                for name in [script, "lock_observations.py"]:
                    self.assertEqual(files[name], (TOOLS / name).read_text())
                if stem == "ovlock-janitor":
                    self.assertEqual(
                        files["s3lite.py"],
                        (TOOLS.parent / "exporters/s3lite.py").read_text(),
                    )
                for name, contents in files.items():
                    Path(tmp, name).write_text(contents)
                result = subprocess.run(
                    [sys.executable, "-E", "-B", str(Path(tmp, script)), "--help"],
                    cwd=tmp,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
