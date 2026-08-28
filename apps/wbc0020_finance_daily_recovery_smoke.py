#!/usr/bin/env python3
"""Fixture-only smoke for sequential one-submit WBC0020 orchestration."""

from __future__ import annotations

import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps import wbc0020_finance_daily_recovery as operation  # noqa: E402


DEPLOYED_SHA = "a" * 40


def main() -> None:
    calls: list[tuple[str, str]] = []
    readback_counts = {date: 0 for date in operation.RECOVERY_DATES}

    def fake_run(arguments: list[str], *, allow_failure: bool = False) -> dict:
        action = arguments[0]
        if action == "finance-daily-parity":
            target_date = arguments[arguments.index("--target-date") + 1]
            calls.append(("parity", target_date))
            return {
                "result": {
                    "target_date": target_date,
                    "deployed_sha": DEPLOYED_SHA,
                    "parity_status": "exact",
                    "changed_cells": 0,
                    "expected_target_cells": 171,
                    "before_plan_digest": "sha256:" + "b" * 64,
                    "source": {
                        "source_digest": "sha256:" + "c" * 64,
                        "pages": 1,
                        "terminal_cursor": 123,
                        "coverage": "33/33",
                    },
                }
            }
        if action == "finance-daily-recovery-plan":
            target_date = arguments[arguments.index("--target-date") + 1]
            output = Path(arguments[arguments.index("--output") + 1])
            calls.append(("plan", target_date))
            plan = {
                "contract_name": "finance_daily_historical_recovery",
                "mode": "recovery",
                "target_date": target_date,
                "deployed_sha": DEPLOYED_SHA,
                "apply_allowed": True,
                "expected_target_cells": 171,
                "fingerprint": "sha256:" + target_date.replace("-", "") + "d" * 48,
                "operation_id": "fixture-" + target_date,
                "before_plan_digest": "sha256:" + "e" * 64,
                "after_plan_digest": "sha256:" + "f" * 64,
                "non_target_digest": "sha256:" + "1" * 64,
                "changed_cells": 171,
                "source": {
                    "source_digest": "sha256:" + "2" * 64,
                    "pages": 1,
                    "terminal_cursor": 456,
                    "terminal_status": 204,
                    "complete": True,
                    "coverage": "33/33",
                },
            }
            operation._write_private(output, plan)
            return {"result": {"status": "planned"}}
        if action == "finance-daily-recovery-apply":
            target_date = arguments[arguments.index("--target-date") + 1]
            calls.append(("apply", target_date))
            if target_date == "2026-08-26" and allow_failure:
                return {"status": "transport_ambiguous", "return_code": 1}
            return {"result": {"status": "applied"}}
        if action == "finance-daily-recovery-readback":
            operation_id = arguments[arguments.index("--operation-id") + 1]
            target_date = operation_id.removeprefix("fixture-")
            calls.append(("readback", target_date))
            readback_counts[target_date] += 1
            return {
                "result": {
                    "status": "complete",
                    "operation_id": operation_id,
                    "target_date": target_date,
                    "accepted_cells": "171/171",
                    "coverage": "33/33",
                    "query_only": True,
                    "checks": {"all": True},
                }
            }
        raise AssertionError(f"unexpected hosted action: {arguments}")

    with TemporaryDirectory(prefix="wbc0020-outer-") as tmp:
        previous = os.environ.get("RUNNER_TEMP")
        os.environ["RUNNER_TEMP"] = tmp
        original = operation._run_hosted
        operation._run_hosted = fake_run
        try:
            preflight = operation.dry_run()
            assert preflight["status"] == "ready"
            result = operation.apply()
            assert result["status"] == "complete"
            assert result["accepted_cells"] == "342/342"
            first_apply_calls = [item for item in calls if item[0] == "apply"]
            assert first_apply_calls == [
                ("apply", "2026-08-26"),
                ("apply", "2026-08-27"),
            ]
            first_plan_calls = [item for item in calls if item[0] == "plan"]
            assert first_plan_calls == [
                ("plan", "2026-08-26"),
                ("plan", "2026-08-27"),
            ]
            assert calls.index(("readback", "2026-08-26")) < calls.index(
                ("plan", "2026-08-27")
            )
            assert readback_counts == {
                "2026-08-26": 1,
                "2026-08-27": 1,
            }

            repeated = operation.apply()
            assert repeated["status"] == "complete"
            assert [item for item in calls if item[0] == "apply"] == first_apply_calls
            assert [item for item in calls if item[0] == "plan"] == first_plan_calls
            assert readback_counts == {
                "2026-08-26": 2,
                "2026-08-27": 2,
            }
        finally:
            operation._run_hosted = original
            if previous is None:
                os.environ.pop("RUNNER_TEMP", None)
            else:
                os.environ["RUNNER_TEMP"] = previous

    print("wbc0020_finance_daily_recovery_smoke: OK")


if __name__ == "__main__":
    main()
