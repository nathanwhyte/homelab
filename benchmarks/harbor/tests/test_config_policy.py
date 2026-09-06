import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HARBOR_DIR = Path(__file__).resolve().parents[1]
CHECKER = HARBOR_DIR / "check-config-policy.py"
REQUIRED_INSTRUCTION = "instructions/bug-1068-readiness.md"
COMPLETE_POLICY = (
    "Run a foreground shell command that checks existence and content. "
    "Wait for a visible shell prompt. Inspect the output before `task_complete`."
)


class ConfigPolicyTests(unittest.TestCase):
    def run_checker(self, root: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(CHECKER), "--root", str(root)],
            text=True,
            capture_output=True,
            check=False,
        )

    def make_root(self, config: str, instruction: str | None = COMPLETE_POLICY) -> Path:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        root = Path(temp_dir.name)
        (root / "configs").mkdir()
        (root / "instructions").mkdir()
        (root / "configs" / "benchmark.yaml").write_text(config)
        if instruction is not None:
            (root / REQUIRED_INSTRUCTION).write_text(instruction)
        return root

    def test_rejects_config_without_bug_1068_instruction(self):
        root = self.make_root(
            "job_name: unsafe\n"
            "orchestrator:\n"
            "  n_concurrent_trials: 1\n"
            "agents:\n"
            "  - name: terminus-2\n"
            "    kwargs:\n"
            "      record_terminal_session: false\n"
        )

        result = self.run_checker(root)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("extra_instruction_paths", result.stderr)

    def test_rejects_more_than_one_local_model_consumer(self):
        root = self.make_root(
            "job_name: unsafe\n"
            "extra_instruction_paths:\n"
            f"  - {REQUIRED_INSTRUCTION}\n"
            "orchestrator:\n"
            "  n_concurrent_trials: 2\n"
            "agents:\n"
            "  - name: terminus-2\n"
            "    kwargs:\n"
            "      record_terminal_session: false\n"
        )

        result = self.run_checker(root)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("n_concurrent_trials must be 1", result.stderr)

    def test_accepts_required_instruction_and_serial_execution(self):
        root = self.make_root(
            "job_name: safe\n"
            "extra_instruction_paths:\n"
            f"  - {REQUIRED_INSTRUCTION}\n"
            "orchestrator:\n"
            "  n_concurrent_trials: 1\n"
            "agents:\n"
            "  - name: terminus-2\n"
            "    kwargs:\n"
            "      record_terminal_session: false\n"
        )

        result = self.run_checker(root)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("1 config", result.stdout)

    def test_rejects_incomplete_readiness_instruction(self):
        root = self.make_root(
            "job_name: unsafe\n"
            "extra_instruction_paths:\n"
            f"  - {REQUIRED_INSTRUCTION}\n"
            "orchestrator:\n"
            "  n_concurrent_trials: 1\n",
            instruction="Remember to verify the task.",
        )

        result = self.run_checker(root)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing required completion language", result.stderr)

    def test_checked_in_configs_satisfy_policy(self):
        result = self.run_checker(HARBOR_DIR)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("4 config", result.stdout)


if __name__ == "__main__":
    unittest.main()
