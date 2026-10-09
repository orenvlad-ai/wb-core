"""Exercise trusted command budgets without running native history fixtures."""

import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_checks
from select_checks import PLAN_SCHEMA, canonical_bytes, digest


class CommandBudgetTests(unittest.TestCase):
    def run_plan(self, commands, *, fail=None):
        calls = []
        head = "a" * 40
        with TemporaryDirectory(prefix="trusted-check-budget-") as directory:
            root = Path(directory).resolve()
            plan = {
                "schema": PLAN_SCHEMA,
                "base_sha": "b" * 40,
                "head_sha": head,
                "release_kind": "repo_only",
                "changed_paths": [],
                "commands": commands,
                # A candidate cannot extend its execution budget through a plan.
                "timeout_seconds": 999999,
            }
            plan["plan_sha256"] = digest(canonical_bytes(plan))
            path = root / "plan.json"
            path.write_text(json.dumps(plan), encoding="utf-8")

            def invoke(command, **kwargs):
                self.assertEqual(kwargs["cwd"], root)
                self.assertTrue(kwargs["check"])
                if command == ["git", "rev-parse", "HEAD"]:
                    return SimpleNamespace(stdout=head + "\n")
                calls.append((command, kwargs["timeout"]))
                if fail is not None:
                    raise fail(command, kwargs["timeout"])
                return SimpleNamespace(returncode=0)

            with patch.object(sys, "argv", ["run_checks", "--root", directory, "--plan", str(path)]), patch.object(run_checks.subprocess, "run", side_effect=invoke):
                if fail is None:
                    self.assertEqual(run_checks.main(), 0)
                else:
                    with self.assertRaises(subprocess.TimeoutExpired):
                        run_checks.main()
        return calls

    def test_exact_full_suite_gets_aggregate_budget(self):
        command = ["python3", "apps/operator_supplier_history_smoke.py"]
        self.assertEqual(self.run_plan([command]), [(command, 1800)])

    def test_lookalikes_and_other_commands_keep_default(self):
        commands = [
            ["python3", "apps/operator_supplier_history_smoke.py", "Tests"],
            ["python3", "./apps/operator_supplier_history_smoke.py"],
            ["python3", "apps/operator_supplier_history_cycle_smoke.py"],
            ["node", "fixture.js"],
        ]
        self.assertEqual(self.run_plan(commands), [(command, 900) for command in commands])

    def test_timeout_still_fails_and_stops_remaining_commands(self):
        command = ["python3", "apps/operator_supplier_history_smoke.py"]
        calls = self.run_plan(
            [command, ["python3", "apps/not_reached_smoke.py"]],
            fail=lambda command, timeout: subprocess.TimeoutExpired(command, timeout),
        )
        self.assertEqual(calls, [(command, 1800)])


if __name__ == "__main__":
    unittest.main()
