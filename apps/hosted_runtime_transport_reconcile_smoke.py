"""Regression matrix for bounded exact-SHA transport reconciliation."""

from __future__ import annotations

import json
import hashlib
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
DEPLOYED_AT = "2026-08-21T05:00:00Z"


def _metadata_sha(*, complete: bool, sha: str = MERGE) -> str:
    payload = {
        "schema_version": "wb_core_deploy_metadata_v2",
        "commit": sha,
        "deployed_at": DEPLOYED_AT,
        "deployment_complete": complete,
    }
    raw = (
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


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
    deployment_complete: bool = True,
    unit: str = "active",
    pid: int = 42,
    probes: str = "401,303",
) -> dict[str, object]:
    return {
        "metadata_sha": metadata,
        "runtime_sha": runtime,
        "metadata_schema_version": "wb_core_deploy_metadata_v2",
        "metadata_deployed_at": DEPLOYED_AT,
        "metadata_sha256": _metadata_sha(
            complete=deployment_complete,
            sha=metadata,
        ),
        "runtime_sha256": hashlib.sha256((runtime + "\n").encode("utf-8")).hexdigest(),
        "deployment_complete": deployment_complete,
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
        if ".safe-finalize." in shell:
            assert "rsync" not in shell
            assert "pip install" not in shell
            assert "systemctl restart" not in shell
            self.operations.append("safe-finalize")
            return _result()
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


def _run(
    runner: ScenarioRunner,
    *,
    stage: str = "readback",
    allow_repairs: bool = True,
    allow_safe_finalize: bool = False,
) -> dict[str, object]:
    return reconcile(
        target_file=CANONICAL,
        expected_sha=MERGE,
        pr=668,
        head=HEAD,
        merge=MERGE,
        failed_stage=stage,
        attempts=3,
        allow_repairs=allow_repairs,
        allow_safe_finalize=allow_safe_finalize,
        runner=runner,
        sleep=lambda _: None,
    )


def main() -> None:
    assert classify_disconnect(255) == "transport-indeterminate"
    assert classify_disconnect(1) == "failed"

    healthy = ScenarioRunner([_result(payload=_evidence())])
    assert _run(healthy)["healthy"] is True
    assert healthy.operations == ["readback"]

    incomplete = ScenarioRunner(
        [
            _result(payload=_evidence(deployment_complete=False)),
            _result(payload=_evidence()),
        ]
    )
    result = _run(incomplete)
    assert result["status"] == "reconciled" and result["healthy"] is True
    assert incomplete.operations == ["readback", "readback"]

    safe_finalize = ScenarioRunner(
        [
            _result(payload=_evidence(deployment_complete=False)),
            _result(payload=_evidence(deployment_complete=True)),
        ]
    )
    result = _run(
        safe_finalize,
        allow_repairs=False,
        allow_safe_finalize=True,
    )
    assert result["status"] == "reconciled" and result["healthy"] is True
    assert result["safe_finalize_applied"] is True
    assert result["safe_finalize_plan"]["expected_effects"] == {
        "metadata_completion_cas_count": 1,
        "rsync_count": 0,
        "dependency_install_count": 0,
        "service_restart_count": 0,
        "business_data_mutation_count": 0,
        "post_metadata_sha256": _metadata_sha(complete=True),
    }
    assert str(result["safe_finalize_plan"]["fingerprint"]).startswith("sha256:")
    assert safe_finalize.operations == ["readback", "safe-finalize", "readback"]

    # A transport disconnect during the single CAS is reconciled only by
    # query-only post-readback; the CAS is never submitted twice.
    finalize_disconnect = ScenarioRunner(
        [
            _result(payload=_evidence(deployment_complete=False)),
            _result(payload=_evidence(deployment_complete=True)),
        ]
    )
    original_call = finalize_disconnect.__call__
    finalize_calls = 0

    def disconnecting_finalize(command: list[str]) -> subprocess.CompletedProcess[str]:
        nonlocal finalize_calls
        if ".safe-finalize." in command[-1]:
            finalize_calls += 1
            finalize_disconnect.operations.append("safe-finalize")
            return _result(255)
        return original_call(command)

    result = reconcile(
        target_file=CANONICAL,
        expected_sha=MERGE,
        pr=668,
        head=HEAD,
        merge=MERGE,
        failed_stage="readback",
        attempts=3,
        allow_repairs=False,
        allow_safe_finalize=True,
        runner=disconnecting_finalize,
        sleep=lambda _: None,
    )
    assert result["healthy"] is True and finalize_calls == 1
    assert finalize_disconnect.operations == ["readback", "safe-finalize", "readback"]

    drift_payload = _evidence(deployment_complete=False)
    drift_payload["metadata_sha256"] = "invalid"
    drift = ScenarioRunner([_result(payload=drift_payload)])
    drift_result = _run(
        drift,
        allow_repairs=False,
        allow_safe_finalize=True,
    )
    assert drift_result["healthy"] is False
    assert drift.operations == ["readback"]

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

    read_only = ScenarioRunner(
        [
            _result(payload=_evidence(unit="inactive", pid=0, probes="000")),
            _result(payload=_evidence(unit="inactive", pid=0, probes="000")),
            _result(payload=_evidence(unit="inactive", pid=0, probes="000")),
        ]
    )
    read_only_result = _run(read_only, allow_repairs=False)
    assert read_only_result["healthy"] is False
    assert read_only_result["read_only"] is True
    assert read_only_result["repairs_applied"] is False
    assert read_only.operations == ["readback", "readback", "readback"]

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
    already_complete = ScenarioRunner([_result(payload=_evidence())])
    result = _run(
        already_complete,
        allow_repairs=False,
        allow_safe_finalize=True,
    )
    assert result["healthy"] is True
    assert result["safe_finalize_applied"] is False
    assert already_complete.operations == ["readback"]

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
