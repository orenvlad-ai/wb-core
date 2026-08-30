#!/usr/bin/env python3
"""Static one-submit/manifest smoke for WBC0027 release orchestration."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps import wbc0027_capital_recovery_release as operation  # noqa: E402
from apps.release_protocol import validate_production_manifest  # noqa: E402


def _plan() -> dict:
    return {
        "contract_name": "wbc0027_capital_recovery_v1",
        "status": "ready",
        "production_mutation_count": 0,
        "deployed_sha": "a" * 40,
        "plan_fingerprint": "sha256:" + "b" * 64,
        "product_operation_id": "recovery_" + "1" * 32,
        "economics_operation_id": "recovery_" + "2" * 32,
        "product_capital": {
            "before_target_digest": "sha256:0e29a9f06148b6fb9102f5f37db7522523b1202c7d36751023efe9831e56e94a",
            "counts": {
                "primary_row_count": 936,
                "primary_cell_count": 19656,
                "primary_mismatch_count": 7655,
                "event_path_mismatch_count": 7639,
                "separate_20260821_mismatch_count": 16,
                "secondary_mismatch_count": 1791,
            }
        },
        "functional_economics": {
            "before_digest": "sha256:529850e0be1d1518f6f6de2f32f650206c6afbf73a093df81359cf42d3e21253",
            "logical_repair_count": 298,
            "persisted_repair_count": 472,
            "evidence_blocked": [f"blocked-{index}" for index in range(12)],
        },
    }


def main() -> None:
    manifest_path = ROOT / "release" / "production-mutations" / "wbc0027_capital_recovery.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    validation = validate_production_manifest(manifest)
    assert validation["valid"], validation
    assert manifest["operation_id"] == "wbc0027-product-capital-and-qualified-economics-v1"
    assert manifest["expected_affected_record_count"] == 1155

    with tempfile.TemporaryDirectory(prefix="wbc0027-release-smoke-") as temp:
        original_temp = os.environ.get("RUNNER_TEMP")
        original_run = operation._run
        calls: list[tuple[str, str]] = []
        completed: set[str] = set()

        def fake_run(arguments: list[str], *, allow_failure: bool = False) -> dict:
            phase = arguments[arguments.index("--phase") + 1] if "--phase" in arguments else ""
            calls.append((arguments[0], phase))
            if arguments[0] == "wbc0027-capital-recovery-plan":
                plan = _plan()
                output = Path(arguments[arguments.index("--output") + 1])
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(json.dumps(plan, sort_keys=True), encoding="utf-8")
                return plan
            if arguments[0] == "wbc0027-capital-recovery-apply":
                assert allow_failure is True
                completed.add(phase)
                return {"status": "transport_ambiguous", "return_code": 255}
            assert arguments[0] == "wbc0027-capital-recovery-readback"
            product_exact = "product" in completed
            economics_exact = "economics" in completed
            return {
                "status": "reconciled" if product_exact and economics_exact else "pending_reconciliation",
                "query_only": True,
                "product_operation_id": _plan()["product_operation_id"],
                "economics_operation_id": _plan()["economics_operation_id"],
                "product_exact": product_exact,
                "economics_exact": economics_exact,
                "product_recovery_lifecycle": "retained" if product_exact else "missing",
                "economics_recovery_lifecycle": "retained" if economics_exact else "missing",
            }

        try:
            os.environ["RUNNER_TEMP"] = temp
            operation._run = fake_run
            dry = operation.dry_run()
            assert dry["production_mutation_count"] == 0
            assert dry["product_counts"]["primary_mismatch_count"] == 7655
            applied = operation.apply()
            assert applied["status"] == "reconciled"
            repeated = operation.apply()
            assert repeated["status"] == "reconciled"
            assert calls.count(("wbc0027-capital-recovery-apply", "product")) == 1, calls
            assert calls.count(("wbc0027-capital-recovery-apply", "economics")) == 1, calls
            assert calls.count(("wbc0027-capital-recovery-readback", "")) == 4, calls
        finally:
            operation._run = original_run
            if original_temp is None:
                os.environ.pop("RUNNER_TEMP", None)
            else:
                os.environ["RUNNER_TEMP"] = original_temp
    print("wbc0027_capital_recovery_smoke: OK")


if __name__ == "__main__":
    main()
