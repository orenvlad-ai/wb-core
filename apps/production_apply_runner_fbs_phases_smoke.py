#!/usr/bin/env python3
"""Ordered dependency-isolated regression for the five FBS v2 phases."""

from __future__ import annotations

import argparse
import contextlib
import copy
import io
import json
import os
import tempfile
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
import sys

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps import production_apply_runner as apply


REPOSITORY = "orenvlad-ai/wb-core"
SOURCE_PR = 1143
CORRECTION_PR = 1145
SOURCE_OPERATION = "release-v2-b3cbca1ace1f88413a5da5be0c7ce4dd"
CORRECTION_OPERATION = "release-v2-76858aebf78533adc107428d99a7aa33"
SOURCE_MERGE = "1d3a4c6074157d4f5e040846da3c61f5506e8797"
CORRECTION_MERGE = "5cdd45b5a499e630bed5277d46bd7047ac6624e2"
MAPPING_COMMENT_ID = 6101
RECOVERY_COMMENT_ID = 6102
MAPPING_READBACK_DIGEST = "sha256:" + "8" * 64
IMPACT_DIGEST = "sha256:" + "9" * 64
RECOVERY_DIGEST = "sha256:" + "a" * 64


def _release_bindings() -> tuple[dict[str, object], dict[str, object]]:
    manifest_path = "release/production-mutations/wbc0027_fbs_lifecycle_incident.json"
    source_receipt = {
        "base_sha": "3a3b7b31b38a1670c4409bb534677b81b0b02168",
        "deployed_sha": SOURCE_MERGE,
        "head_sha": "fca1c66d1d5f010e762b3fc94505448c90aa6c23",
        "manifest": {
            "operation_id": "wbc0027-fbs-identity-428855758-v2",
            "path": manifest_path,
            "sha256": apply.digest((ROOT / manifest_path).read_bytes()),
        },
        "merge_sha": SOURCE_MERGE,
        "operation_id": SOURCE_OPERATION,
        "plan_hash": "b63a646506e5051aa214b007a99e4494850a4f7665352a914d87452110b9a261",
        "pull_request": SOURCE_PR,
        "reason_codes": [],
        "release_kind": "production_mutation",
        "repository": REPOSITORY,
        "schema": "wb-core.release-receipt/v2",
        "state": "awaiting_apply",
        "workflow_run_id": 33414596664,
    }
    correction_receipt = {
        "base_sha": "e5adff55cd5f2f6581ab724984ee8ab3b14a0e09",
        "deployed_sha": CORRECTION_MERGE,
        "head_sha": "068446766a144348578cd8460d8f22f267460681",
        "manifest": None,
        "merge_sha": CORRECTION_MERGE,
        "operation_id": CORRECTION_OPERATION,
        "plan_hash": "f4acaa5917f132bf7bd98d68a07f2cf82202b6cdf80c128d37e69b474080fb8c",
        "pull_request": CORRECTION_PR,
        "reason_codes": [],
        "release_kind": "live_runtime",
        "repository": REPOSITORY,
        "schema": "wb-core.release-receipt/v2",
        "state": "done",
        "workflow_run_id": 33434060381,
    }
    source_raw = apply.canonical_json_bytes(source_receipt) + b"\n"
    correction_raw = apply.canonical_json_bytes(correction_receipt) + b"\n"
    assert apply.digest(correction_raw) == (
        "1595293c9cd55df7aa36a09bc278c3d260a554d3b0a8c9109da9a89562a49d92"
    )
    source = {
        "receipt": source_receipt,
        "comment_id": 5481503347,
        "gate_run_id": 33414596664,
        "release_run_id": 33415566222,
        "artifact_id": 9767013211,
        "artifact_archive_digest": "sha256:" + "1" * 64,
        "artifact_file_sha256": "sha256:" + apply.digest(source_raw),
    }
    correction = {
        "receipt": correction_receipt,
        "comment_id": 5484024408,
        "gate_run_id": 33434060381,
        "release_run_id": 33435006142,
        "artifact_id": 9774197000,
        "artifact_archive_digest": "sha256:" + "2" * 64,
        "artifact_file_sha256": "sha256:" + apply.digest(correction_raw),
    }
    return source, correction


def _authorization(comment_id: int, body: str) -> dict[str, object]:
    return {
        "id": comment_id,
        "author_association": "OWNER",
        "issue_url": f"https://api.github.com/repos/{REPOSITORY}/issues/{SOURCE_PR}",
        "user": {"login": "owner"},
        "body": body,
    }


class Client:
    repository = REPOSITORY

    def __init__(self, comments: list[dict[str, object]]) -> None:
        self.comments = comments
        self.artifacts: dict[int, dict[str, object]] = {}
        self.next_comment_id = 7100
        self.events: list[tuple[str, int]] = []

    def install_artifact(self, run_id: int, receipt_path: Path) -> None:
        raw = receipt_path.read_bytes()
        archive_buffer = io.BytesIO()
        with zipfile.ZipFile(archive_buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(apply.RECOVERY_ARTIFACT_FILE, raw)
        raw_zip = archive_buffer.getvalue()
        name = apply._receipt_artifact_name(SOURCE_PR, run_id)
        self.artifacts[run_id] = {
            "metadata": {
                "id": run_id + 9000,
                "name": name,
                "expired": False,
                "digest": "sha256:" + apply.digest(raw_zip),
                "workflow_run": {"id": run_id, "head_sha": CORRECTION_MERGE},
            },
            "raw_zip": raw_zip,
        }
        self.events.append(("artifact", run_id))

    def get(self, path: str):
        if path.startswith("/actions/runs/") and "/artifacts?" in path:
            run_id = int(path.split("/")[3])
            artifact = self.artifacts.get(run_id)
            return {
                "artifacts": [copy.deepcopy(artifact["metadata"])] if artifact else []
            }
        raise AssertionError(path)

    def request(self, method: str, path: str, **_kwargs):
        assert method == "GET"
        artifact_id = int(path.split("/")[3])
        matches = [
            (run_id, item)
            for run_id, item in self.artifacts.items()
            if int(item["metadata"]["id"]) == artifact_id
        ]
        assert len(matches) == 1
        run_id, item = matches[0]
        self.events.append(("download", run_id))
        return item["raw_zip"]

    def post(self, path: str, payload: dict[str, object]):
        assert path == f"/issues/{SOURCE_PR}/comments"
        body = str(payload["body"])
        run_id = int(body.split("production-apply-receipt-pr-1143-run-")[1].split('"')[0])
        self.events.append(("marker", run_id))
        comment = {
            "id": self.next_comment_id,
            "author_association": "NONE",
            "issue_url": f"https://api.github.com/repos/{REPOSITORY}/issues/{SOURCE_PR}",
            "user": {"login": "github-actions[bot]"},
            "body": body,
        }
        self.next_comment_id += 1
        self.comments.append(comment)
        return comment


def _args(
    *,
    mode: str,
    authorization_comment_id: int,
    predecessor_comment_id: int,
    output: Path,
    execution_phase: str,
    manifest_sha256: str = "",
) -> argparse.Namespace:
    return argparse.Namespace(
        authorization_mode=mode,
        repository=REPOSITORY,
        pr=SOURCE_PR,
        release_operation_id=SOURCE_OPERATION,
        reconciliation_pr=CORRECTION_PR,
        reconciliation_release_operation_id=CORRECTION_OPERATION,
        authorization_comment_id=authorization_comment_id,
        blocked_comment_id=predecessor_comment_id,
        manifest_sha256=manifest_sha256,
        execution_phase=execution_phase,
        output=output,
        github_output=None,
    )


def _expect_blocked(callable_object, message: str) -> None:
    try:
        callable_object()
    except apply.ApplyError:
        return
    raise AssertionError(message)


def _run_mode(*, args: argparse.Namespace, client: Client) -> int:
    with contextlib.redirect_stdout(io.StringIO()):
        return apply._run_wbc0027_fbs_v2_mode(
            args=args, client=client, comments=list(client.comments)
        )


def main() -> None:
    assert "openpyxl" not in sys.modules
    passport = apply._load_fbs_incident_passport()
    passport_digest = apply.fbs_file_digest(apply.WBC0027_FBS_INCIDENT_PASSPORT_PATH)
    mapping_body = (
        "/wb-core authorize-goal-v2 task WBC0027 profile fbs-identity-mapping-v2 "
        "target wb_core_eu_hosted_runtime_active incident-passport "
        f"{passport_digest} operation {passport['operation_id']} inserts 1 submits 1"
    )
    mapping_comment = _authorization(MAPPING_COMMENT_ID, mapping_body)
    mapping_goal = apply.validate_authorization(
        mapping_comment, repository=REPOSITORY, pr=SOURCE_PR
    )
    mapping_root = apply.operation_id(
        REPOSITORY, SOURCE_PR, MAPPING_COMMENT_ID, mapping_goal
    )
    recovery_body = (
        "/wb-core authorize-goal-v2 task WBC0027 profile fbs-lifecycle-recovery-v2 "
        "target wb_core_eu_hosted_runtime_active incident-passport "
        f"{passport_digest} mapping-operation {mapping_root} "
        f"mapping-readback {MAPPING_READBACK_DIGEST} impact {IMPACT_DIGEST} "
        f"recovery {RECOVERY_DIGEST} submits 1"
    )
    recovery_comment = _authorization(RECOVERY_COMMENT_ID, recovery_body)
    client = Client([mapping_comment, recovery_comment])
    source_release, correction_release = _release_bindings()
    calls: list[tuple[str, str]] = []

    original_collect = apply.collect_exact_release_binding
    original_ancestry = apply.collect_correction_base_ancestry
    original_subprocess_run = apply.subprocess.run
    original_target = apply._canonical_target
    original_configure = apply.configure_deploy_environment
    original_mapping = apply.run_wbc0027_fbs_mapping_goal
    original_impact = apply.run_wbc0027_fbs_impact_generation
    original_quality = apply.run_wbc0027_fbs_quality_goal
    original_run_id = os.environ.get("GITHUB_RUN_ID")
    mapping_qualification_binding = {
        "fingerprint": "sha256:" + "3" * 64,
        "material_cas_digest": "sha256:" + "5" * 64,
        "tuple_digest": str(passport["tuple"]["tuple_digest"]),
    }

    def collect(_client, **kwargs):
        if kwargs["pr"] == SOURCE_PR:
            assert kwargs["release_operation"] == SOURCE_OPERATION
            return copy.deepcopy(source_release)
        assert kwargs["pr"] == CORRECTION_PR
        assert kwargs["release_operation"] == CORRECTION_OPERATION
        return copy.deepcopy(correction_release)

    def mapping_runner(**kwargs):
        mode = "mapping_qualification" if kwargs["qualification_only"] else "mapping_apply"
        calls.append((mode, kwargs["operation"]))
        if kwargs["qualification_only"]:
            return {
                "state": "qualified_no_submit",
                "reason": "submit-boundary-proven",
                "apply_count": 0,
                "production_mutation_count": 0,
                "candidate": dict(mapping_qualification_binding),
            }
        assert {
            key: kwargs["predecessor_qualification"].get(key)
            for key in mapping_qualification_binding
        } == mapping_qualification_binding
        return {
            "state": "done",
            "reason": "reconciled",
            "apply_count": 1,
            "mapping_insert_count": 1,
            "candidate": {"mapping_readback_digest": MAPPING_READBACK_DIGEST},
        }

    def impact_runner(**kwargs):
        calls.append(("impact_generation", kwargs["operation"]))
        assert kwargs["mapping_readback_digest"] == MAPPING_READBACK_DIGEST
        return {
            "state": "qualified_no_submit",
            "reason": "independent-impact-generated",
            "apply_count": 0,
            "production_mutation_count": 0,
            "impact": {
                "path": "/evidence/fbs-impact-manifest.json",
                "digest": IMPACT_DIGEST,
                "mapping_readback_digest": MAPPING_READBACK_DIGEST,
                "mapping_apply_operation_id": kwargs["mapping_apply_operation"],
            },
        }

    def quality_runner(**kwargs):
        mode = "recovery_qualification" if kwargs["qualification_only"] else "recovery_apply"
        calls.append((mode, kwargs["operation"]))
        qualification_binding = {
            "fingerprint": RECOVERY_DIGEST,
            "impact_digest": IMPACT_DIGEST,
            "mapping_apply_operation_id": kwargs["mapping_apply_operation"],
            "impact_generation_operation_id": kwargs[
                "impact_generation_operation"
            ],
            "storage": dict(passport["storage"]),
            "boundary": {
                "mapping_readback_digest": MAPPING_READBACK_DIGEST,
                "storage": dict(passport["storage"]),
            },
            "scope": {
                "groups": ["fixture"],
                "target_count": 1,
                "stable_target_digest": "sha256:" + "6" * 64,
            },
            "history_digest": "sha256:" + "7" * 64,
            "history_classification_counts": {
                "recoverable_exact": 1,
                "remain_missing_no_same_date_evidence": 0,
            },
        }
        if not kwargs["qualification_only"]:
            assert {
                key: kwargs["predecessor_qualification"].get(key)
                for key in qualification_binding
            } == qualification_binding
        result = {
            "state": "qualified_no_submit" if kwargs["qualification_only"] else "done",
            "reason": "submit-boundary-proven" if kwargs["qualification_only"] else "reconciled",
            "apply_count": 0 if kwargs["qualification_only"] else 1,
            "production_mutation_count": 0 if kwargs["qualification_only"] else 1,
            "candidate": qualification_binding,
        }
        return result

    try:
        apply.collect_exact_release_binding = collect
        apply.collect_correction_base_ancestry = lambda *_args, **_kwargs: {
            "schema": "wb-core.fbs-correction-source-ancestry/v1",
            "status": "trusted_non_interfering_descendant",
            "source_merge_sha": SOURCE_MERGE,
            "correction_base_sha": correction_release["receipt"]["base_sha"],
            "intervening_releases": [
                {
                    "pull_request": 1144,
                    "merge_sha": correction_release["receipt"]["base_sha"],
                    "release_kind": "repo_only",
                    "state": "done",
                    "path_proof_digest": "sha256:" + "4" * 64,
                }
            ],
        }
        apply.subprocess.run = lambda *_args, **_kwargs: object()
        apply._canonical_target = lambda: {
            "target_dir": "/opt/wb-core-runtime/app",
            "ssh_destination": "wb-core-eu-root",
        }
        apply.configure_deploy_environment = lambda _directory: None
        apply.run_wbc0027_fbs_mapping_goal = mapping_runner
        apply.run_wbc0027_fbs_impact_generation = impact_runner
        apply.run_wbc0027_fbs_quality_goal = quality_runner

        with tempfile.TemporaryDirectory(prefix="fbs-five-phase-smoke-") as directory:
            root = Path(directory)
            phases = [
                ("fbs-mapping-qualification", MAPPING_COMMENT_ID, ""),
                ("fbs-mapping-apply", MAPPING_COMMENT_ID, ""),
                ("fbs-impact-generation", MAPPING_COMMENT_ID, MAPPING_READBACK_DIGEST),
                ("fbs-recovery-qualification", RECOVERY_COMMENT_ID, ""),
                ("fbs-recovery-apply", RECOVERY_COMMENT_ID, ""),
            ]
            predecessor_comment_id = 0
            receipts: list[dict[str, object]] = []
            marker_ids: list[int] = []
            for index, (mode, authorization_comment_id, manifest_sha) in enumerate(
                phases, start=1
            ):
                run_id = 44000000000 + index
                output = root / f"{index:02d}-{mode}.json"
                args = _args(
                    mode=mode,
                    authorization_comment_id=authorization_comment_id,
                    predecessor_comment_id=predecessor_comment_id,
                    output=output,
                    execution_phase="collect",
                    manifest_sha256=manifest_sha,
                )
                assert _run_mode(args=args, client=client) == 0
                receipt = json.loads(output.read_text(encoding="utf-8"))
                receipts.append(receipt)
                assert receipt["phase"] == apply.FBS_PHASE_BY_MODE[mode]
                assert receipt["operation_id"] != receipt["root_operation_id"]
                assert receipt["apply_count"] == (1 if mode.endswith("-apply") else 0)
                client.install_artifact(run_id, output)
                os.environ["GITHUB_RUN_ID"] = str(run_id)
                args.execution_phase = "publish"
                assert _run_mode(args=args, client=client) == 0
                marker_comment = client.comments[-1]
                marker_ids.append(int(marker_comment["id"]))
                predecessor_comment_id = int(marker_comment["id"])
                publication_events = [
                    event for event in client.events if event[1] == run_id
                ]
                assert publication_events[0] == ("artifact", run_id)
                assert publication_events[-1] == ("marker", run_id)
                assert ("download", run_id) in publication_events[1:-1]
                replay = root / f"{index:02d}-{mode}-replay.json"
                args.output = replay
                args.execution_phase = "collect"
                before_calls = len(calls)
                assert _run_mode(args=args, client=client) == 0
                replay_receipt = json.loads(replay.read_text(encoding="utf-8"))
                assert replay_receipt["state"] == "already_terminal"
                assert replay_receipt["submit_count"] == 0
                assert replay_receipt["ssh_command_count"] == 0
                assert replay_receipt["comment_count"] == 0
                assert len(calls) == before_calls

            phase_operations = [str(item["operation_id"]) for item in receipts]
            assert len(set(phase_operations)) == 5
            assert [item["phase"] for item in receipts] == [
                "mapping_qualification",
                "mapping_apply",
                "impact_generation",
                "recovery_qualification",
                "recovery_apply",
            ]
            assert [item["apply_count"] for item in receipts] == [0, 1, 0, 0, 1]
            assert sum(
                int(item["apply_count"])
                for item in receipts
                if item["phase"] in {
                    "mapping_qualification",
                    "impact_generation",
                    "recovery_qualification",
                }
            ) == 0

            def invalid_run(
                mode: str,
                auth_id: int,
                predecessor_id: int,
                manifest_sha: str = "",
            ) -> None:
                invalid_args = _args(
                    mode=mode,
                    authorization_comment_id=auth_id,
                    predecessor_comment_id=predecessor_id,
                    output=root / "invalid.json",
                    execution_phase="collect",
                    manifest_sha256=manifest_sha,
                )
                _run_mode(args=invalid_args, client=client)

            _expect_blocked(
                lambda: invalid_run("fbs-mapping-apply", MAPPING_COMMENT_ID, 0),
                "mapping Apply skipped qualification",
            )
            _expect_blocked(
                lambda: invalid_run(
                    "fbs-impact-generation",
                    MAPPING_COMMENT_ID,
                    marker_ids[0],
                    MAPPING_READBACK_DIGEST,
                ),
                "impact accepted mapping qualification instead of mapping Apply",
            )
            _expect_blocked(
                lambda: invalid_run(
                    "fbs-recovery-qualification", RECOVERY_COMMENT_ID, marker_ids[1]
                ),
                "recovery qualification accepted a cross-phase marker",
            )
            _expect_blocked(
                lambda: invalid_run(
                    "fbs-impact-generation",
                    MAPPING_COMMENT_ID,
                    marker_ids[1],
                    "sha256:" + "0" * 64,
                ),
                "impact accepted a drifted mapping readback digest",
            )
            mapping_apply_artifact = client.artifacts[44000000002]
            original_mapping_apply_zip = mapping_apply_artifact["raw_zip"]
            mapping_apply_artifact["raw_zip"] = original_mapping_apply_zip + b"drift"
            _expect_blocked(
                lambda: invalid_run(
                    "fbs-impact-generation",
                    MAPPING_COMMENT_ID,
                    marker_ids[1],
                    MAPPING_READBACK_DIGEST,
                ),
                "impact accepted a drifted predecessor artifact archive",
            )
            mapping_apply_artifact["raw_zip"] = original_mapping_apply_zip
            _expect_blocked(
                lambda: apply._collect_fbs_phase_predecessor(
                    client,
                    list(client.comments),
                    pr=SOURCE_PR,
                    comment_id=marker_ids[1],
                    expected_phase="mapping_apply",
                    expected_root_operation_id="production-goal-v2-" + "f" * 32,
                    source_release={
                        **copy.deepcopy(source_release),
                    },
                    correction_release={
                        **copy.deepcopy(correction_release),
                        "source_ancestry": receipts[0]["correction_release"][
                            "source_ancestry"
                        ],
                    },
                    incident_passport_sha256=passport_digest,
                ),
                "foreign root goal was accepted",
            )
            duplicate = {**client.comments[-5], "id": 7999}
            client.comments.append(duplicate)
            _expect_blocked(
                lambda: invalid_run(
                    "fbs-mapping-qualification", MAPPING_COMMENT_ID, 0
                ),
                "duplicate terminal phase marker was accepted",
            )
    finally:
        apply.collect_exact_release_binding = original_collect
        apply.collect_correction_base_ancestry = original_ancestry
        apply.subprocess.run = original_subprocess_run
        apply._canonical_target = original_target
        apply.configure_deploy_environment = original_configure
        apply.run_wbc0027_fbs_mapping_goal = original_mapping
        apply.run_wbc0027_fbs_impact_generation = original_impact
        apply.run_wbc0027_fbs_quality_goal = original_quality
        if original_run_id is None:
            os.environ.pop("GITHUB_RUN_ID", None)
        else:
            os.environ["GITHUB_RUN_ID"] = original_run_id
    assert "openpyxl" not in sys.modules
    print("production_apply_runner_fbs_phases_smoke: ok")


if __name__ == "__main__":
    main()
