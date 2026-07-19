"""Regression matrix for bounded exact-SHA transport reconciliation."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.hosted_runtime_transport_reconcile import classify_disconnect, reconcile


CANONICAL = Path(
    "artifacts/registry_upload_http_entrypoint/input/hosted_runtime_target__europe_api.json"
)
ARCHIVED = Path(
    "artifacts/registry_upload_http_entrypoint/input/hosted_runtime_target__selleros_api.json"
)
HEAD = "1" * 40
MERGE = "2" * 40
OLD = "3" * 40


def _result(returncode: int = 0, payload: dict[str, object] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        ["ssh"],
        returncode,
        stdout=json.dumps(payload or {}),
        stderr="",
    )


def _evidence(
    metadata: str = MERGE,
    runtime: str = MERGE,
    *,
    unit: str = "active",
    pid: int = 42,
    probes: str = "401,303",
) -> dict[str, object]:
    return {
        "metadata_sha": metadata,
        "runtime_sha": runtime,
        "unit": unit,
        "main_pid": str(pid),
        "probe_statuses": probes,
        "target_id": "wb_core_eu_hosted_runtime_active",
        "auth_env_ok": True,
    }


class ScenarioRunner:
    def __init__(self, readbacks: list[subprocess.CompletedProcess[str]]) -> None:
        self.readbacks = list(readbacks)
        self.operations: list[str] = []

    def __call__(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        shell = command[-1]
        if ".wb-core-deploy.json" in shell:
            self.operations.append("readback")
            return self.readbacks.pop(0) if self.readbacks else _result(255)
        if "daemon-reload" in shell:
            self.operations.append("daemon-reload")
        elif "systemctl restart" in shell:
            self.operations.append("restart")
        elif "curl -fsS" in shell:
            self.operations.append("probes")
        return _result()


def _run(runner: ScenarioRunner, *, stage: str = "readback") -> dict[str, object]:
    return reconcile(
        target_file=CANONICAL,
        expected_sha=MERGE,
        pr=668,
        head=HEAD,
        merge=MERGE,
        failed_stage=stage,
        attempts=3,
        runner=runner,
        sleep=lambda _: None,
    )


def main() -> None:
    assert classify_disconnect(255) == "transport-indeterminate"
    assert classify_disconnect(1) == "failed"

    healthy = ScenarioRunner([_result(payload=_evidence())])
    assert _run(healthy)["healthy"] is True
    assert healthy.operations == ["readback"]

    # Disconnect before metadata and after metadata cannot be healed by service retries.
    before_metadata = ScenarioRunner([_result(payload=_evidence(OLD, OLD))])
    result = _run(before_metadata, stage="metadata")
    assert result["status"] == "halted" and result["healthy"] is False
    assert before_metadata.operations == ["readback"]

    after_metadata = ScenarioRunner([_result(payload=_evidence(MERGE, OLD))])
    result = _run(after_metadata, stage="metadata")
    assert result["status"] == "halted" and result["evidence"][0]["runtime_sha"] == OLD

    # daemon-reload/restart/probe uncertainty permits only the bounded safe repair set.
    for stage in ("daemon-reload", "restart", "probes"):
        runner = ScenarioRunner(
            [
                _result(payload=_evidence(unit="inactive", pid=0, probes="000")),
                _result(payload=_evidence()),
            ]
        )
        result = _run(runner, stage=stage)
        assert result["healthy"] is True and result["repairs_applied"] is True
        assert runner.operations == ["readback", "daemon-reload", "restart", "probes", "readback"]

    wrong_sha = ScenarioRunner([_result(payload=_evidence(OLD, OLD))])
    assert _run(wrong_sha)["healthy"] is False
    mixed = ScenarioRunner([_result(payload=_evidence(MERGE, OLD))])
    mixed_result = _run(mixed)
    assert mixed_result["healthy"] is False
    assert mixed_result["evidence"][0]["metadata_sha"] != mixed_result["evidence"][0]["runtime_sha"]
    inactive = ScenarioRunner([_result(payload=_evidence(unit="inactive", pid=0)), _result(payload=_evidence(unit="inactive", pid=0)), _result(payload=_evidence(unit="inactive", pid=0))])
    assert _run(inactive)["healthy"] is False
    missing_auth_payload = _evidence()
    missing_auth_payload["auth_env_ok"] = False
    missing_auth = ScenarioRunner([_result(payload=missing_auth_payload)])
    assert _run(missing_auth)["healthy"] is False
    assert missing_auth.operations == ["readback"]

    # Repeated healthy reconciliation is deterministic and side-effect free.
    first = _run(ScenarioRunner([_result(payload=_evidence())]))
    second = _run(ScenarioRunner([_result(payload=_evidence())]))
    assert first["status"] == second["status"] == "reconciled"
    assert first["expected_sha"] == second["expected_sha"] == MERGE

    try:
        reconcile(
            target_file=ARCHIVED,
            expected_sha=MERGE,
            pr=1,
            head=HEAD,
            merge=MERGE,
            runner=ScenarioRunner([_result(payload=_evidence())]),
        )
    except ValueError:
        pass
    else:
        raise AssertionError("wrong canonical target must fail before SSH")

    print("hosted_runtime_transport_reconcile_smoke: ok")


if __name__ == "__main__":
    main()
