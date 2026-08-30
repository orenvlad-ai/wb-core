#!/usr/bin/env python3
"""JIT, semantic-CAS, and private-persistence smoke for WBC0027."""

from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps import wbc0027_capital_recovery as recovery  # noqa: E402


GOAL = "production-goal-v1-" + "a" * 32
PRODUCT_OPERATION = "recovery_" + "b" * 32
DEPLOYED = "c" * 40
GENERATION = {
    "generation_id": "operational-smoke",
    "manifest_sha256": "sha256:" + "d" * 64,
    "schema_version": 172,
}


def _payload(*, ordinary: str, target: str) -> dict:
    day = "2026-08-26"
    return {
        "date_columns": [day],
        "sheets": [
            {
                "sheet_name": "DATA_VITRINA",
                "header": ["metric", "row_id", day],
                "rows": [
                    ["cost", "428853741|our_wb_unit_cost_rub", target],
                    ["stock", "428853741|warehouse_stock_qty", "ordinary-cell"],
                ],
            }
        ],
        "metadata": {
            "ordinary_publication": ordinary,
            "warehouse_history_coverage": {day: {"functional_version_id": "v1"}},
            "functional_economics_backfill": {
                "inventory_cost_publication": {"date_evidence": {day: {"exact": True}}}
            },
        },
    }


def _economics(ordinary: str) -> dict:
    before = _payload(ordinary=ordinary, target="")
    after = _payload(ordinary=ordinary, target="117.537167")
    return {
        "target_dates": ["2026-08-26", "2026-08-29"],
        "logical_repair_count": 298,
        "persisted_repair_count": 472,
        "patch_count": 1,
        "source_operation_id": recovery.SOURCE_OPERATION_ID,
        "source_digest": recovery.SOURCE_OPERATION_DIGEST,
        "protected_invariant": {
            "as_of_date": "2026-08-26",
            "nm_id": 428853741,
            "unit_cost_rub": "117.537167",
            "status": "separate_exact_invariant_preserved",
        },
        "evidence_blocked": [f"2026-08-26|blocked-{index}" for index in range(12)],
        "patches": [
            {
                "identity": ["bundle", "2026-08-26", "snapshot"],
                "business_dates": ["2026-08-26"],
                "changed_cells": ["2026-08-26|428853741|our_wb_unit_cost_rub"],
                "before_plan_json": json.dumps(before, sort_keys=True),
                "after_plan_json": json.dumps(after, sort_keys=True),
                "before_sha256": recovery._digest(before),
                "after_sha256": recovery._digest(after),
            }
        ],
        "before_digest": recovery._digest(before),
        "after_digest": recovery._digest(after),
        "non_target_digest": recovery._digest({"ordinary": ordinary}),
    }


def _candidate() -> dict:
    economics = _economics("publication-a")
    material = recovery._economics_material(
        economics, product_phase_operation_id=PRODUCT_OPERATION
    )
    candidate = recovery._phase_envelope(
        phase="economics",
        goal_operation_id=GOAL,
        deployed_sha=DEPLOYED,
        generation=GENERATION,
        material=material,
    )
    candidate.update(
        {
            "created_at": "2026-08-31T00:00:00+00:00",
            "functional_economics": economics,
            "product_predecessor": {
                "operation_id": PRODUCT_OPERATION,
                "reconciled": True,
            },
        }
    )
    return candidate


def _admission(*, owner: str, destination: Path, predicted_output_bytes: int) -> dict:
    return {
        "owner": owner,
        "destination": str(destination),
        "destination_role": "backup",
        "predicted_output_bytes": predicted_output_bytes,
        "allowed": True,
    }


def main() -> None:
    assert recovery.EXPECTED_PRODUCT_ROWS == 1152
    assert recovery.EXPECTED_PRODUCT_CELLS == 24192
    assert recovery.EXPECTED_PRODUCT_MISMATCHES == 9446
    assert recovery.EXPECTED_PRIMARY_MISMATCHES == 7655
    assert recovery.EXPECTED_SECONDARY_MISMATCHES == 1791
    assert recovery.EXPECTED_SPECIAL_NM_ID == 497413772
    assert recovery.EXPECTED_SEPARATE_MISMATCHES == 16

    material_a = recovery._economics_material(
        _economics("ordinary-publication-a"),
        product_phase_operation_id=PRODUCT_OPERATION,
    )
    material_b = recovery._economics_material(
        _economics("ordinary-publication-b"),
        product_phase_operation_id=PRODUCT_OPERATION,
    )
    assert recovery._digest(material_a) == recovery._digest(material_b)
    changed = _economics("ordinary-publication-b")
    changed_payload = json.loads(changed["patches"][0]["after_plan_json"])
    changed_payload["sheets"][0]["rows"][0][2] = "999.000000"
    changed["patches"][0]["after_plan_json"] = json.dumps(changed_payload, sort_keys=True)
    assert recovery._digest(
        recovery._economics_material(
            changed, product_phase_operation_id=PRODUCT_OPERATION
        )
    ) != recovery._digest(material_a)

    for legacy in recovery.LEGACY_RELEASE_OPERATION_IDS:
        try:
            recovery._validate_goal_namespace(legacy)
        except recovery.Wbc0027RecoveryError:
            pass
        else:
            raise AssertionError("legacy WBC0027 operation was accepted")

    with tempfile.TemporaryDirectory(prefix="wbc0027-jit-smoke-") as raw:
        root = Path(raw)
        production_goals = root / "production-goals"
        production_goals.mkdir(mode=0o700)
        evidence_dir = production_goals / GOAL
        candidate = _candidate()
        simulated = recovery.publish_candidate(
            candidate=candidate,
            evidence_dir=evidence_dir,
            no_create=True,
            admission_factory=_admission,
        )
        assert simulated["status"] == "ready"
        assert simulated["manifest_path"] == ""
        assert simulated["plan_persistence"]["no_create"] is True
        assert not evidence_dir.exists(), "no-create qualification created evidence state"

        evidence_dir.mkdir(mode=0o700)

        def writer(path: Path, payload: dict, **kwargs: object) -> dict:
            return recovery._write_private(
                path,
                payload,
                admission_factory=_admission,
                **kwargs,
            )

        persisted = recovery.publish_candidate(
            candidate=deepcopy(candidate),
            evidence_dir=evidence_dir,
            no_create=False,
            writer=writer,
        )
        manifest = Path(persisted["manifest_path"])
        assert manifest.is_file()
        assert manifest.stat().st_mode & 0o777 == 0o600
        assert evidence_dir.stat().st_mode & 0o777 == 0o700
        receipt = persisted["plan_persistence"]
        assert receipt["atomic_publish"] is True
        assert receipt["no_overwrite"] is True
        assert receipt["durable_file_fsync"] is True
        assert receipt["durable_directory_fsync"] is True
        assert persisted["manifest_sha256"] == recovery._file_digest(manifest)

    wrapper = subprocess.run(
        [sys.executable, str(ROOT / "apps/wbc0027_capital_recovery_release.py"), "apply"],
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "RUNNER_TEMP": ""},
    )
    blocked = json.loads(wrapper.stdout)
    assert wrapper.returncode == 1
    assert blocked["reason"] == "historical_superseded_non_runnable"
    assert blocked["production_mutation_submit_count"] == 0
    print("wbc0027_capital_recovery_smoke: OK")


if __name__ == "__main__":
    main()
