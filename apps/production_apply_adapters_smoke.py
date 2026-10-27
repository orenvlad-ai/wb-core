#!/usr/bin/env python3
"""Offline registration and envelope checks for production adapters."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sqlite3
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.production_apply_adapters import ADAPTERS  # noqa: E402
from apps.production_apply_contract import AdapterError, AmbiguousSubmit  # noqa: E402
from apps import wb_fbs_mapping_evidence_production_adapter as adapter_module  # noqa: E402
from apps.wb_fbs_mapping_evidence_production_adapter import (  # noqa: E402
    REMOTE_APP,
    REMOTE_ENV_FILE,
    REMOTE_RUNTIME_DIR,
    WbFbsMappingEvidenceProductionAdapter,
)


class Harness(WbFbsMappingEvidenceProductionAdapter):
    def __init__(self) -> None:
        self.calls = []

    def _invoke(self, **kwargs):
        self.calls.append(kwargs)
        action = kwargs["action"]
        if action == "preview":
            return {
                "operation_id": kwargs["operation_id"],
                "target": "fixture",
                "scope": {"row_count": 1},
                "prestate_sha256": "sha256:" + "1" * 64,
                "candidate_sha256": "sha256:" + "2" * 64,
                "recovery": {"kind": "append_only_reversion"},
            }
        if action == "readback":
            return {"operation_id": kwargs["operation_id"], "state": "not_submitted"}
        return {"operation_id": kwargs["operation_id"], "disposition": "submitted"}


def finance_stale_adapter_contract():
    from tempfile import TemporaryDirectory
    from unittest.mock import patch, Mock
    from apps.wb_finance_weekly import FinanceStaleCostAdapter
    from packages.application.wb_finance_payout_apply import digest
    from apps.production_apply_launcher import execute
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        request = {'runtime_dir':tmp}
        plan = {'fingerprint':'sha256:'+'1'*64, 'target_before_digest':'sha256:'+'2'*64,
                'seller_id':'test', 'date_from':'2026-10-12','date_to':'2026-10-25',
                'weeks':[{'week_start':'2026-10-12','week_end':'2026-10-18'}]}
        result = {'fingerprint':plan['fingerprint'],'non_target_digest_after':'same',
                  'target_image_digest':digest({}), 'weeks':plan['weeks'],
                  'source_advanced_after_apply':False,'post_verify_stale_week_count':0}
        block = Mock(runtime_dir=root,db_path=root/'db')
        block.apply_stale_cost_weeks.return_value = result
        block._finance_target_images.return_value = {}
        block._json_digest.side_effect = digest
        adapter = FinanceStaleCostAdapter()
        adapter._bound = Mock(return_value=(block,plan))
        conn = sqlite3.connect(':memory:')
        registry = Mock()
        registry.connect.return_value = conn
        with patch('packages.application.wb_finance_payout_apply.target', return_value=(registry,None)), \
                patch('apps.wb_finance_weekly.WarehouseRecoveryRegistry', return_value=registry), \
                patch('apps.wb_finance_weekly._prepare_bounded_finance_recovery', return_value={'operation_id':'recover','lifecycle':'mutating'}):
            preview = adapter.preview(request,'finance-test')
            receipt = execute(action='apply',adapter_name='finance_test',operation_id='finance-test',
                request=request,expected_prestate=preview['prestate_sha256'],expected_candidate=preview['candidate_sha256'],adapters={'finance_test':adapter})
            assert receipt['state']=='applied'
            block.apply_stale_cost_weeks.assert_called_once()
        # An interrupted prior submit can only be read back; it cannot replay.
        path = adapter._path(request,'finance-interrupted')
        path.write_text(json.dumps({'request_digest':digest(request),'plan_fingerprint':plan['fingerprint']}))
        assert adapter.readback(request,'finance-interrupted')['state']=='ambiguous'
        try:
            execute(action='apply',adapter_name='finance_test',operation_id='finance-interrupted',request=request,
                    expected_prestate=preview['prestate_sha256'],expected_candidate=preview['candidate_sha256'],adapters={'finance_test':adapter})
        except Exception as exc:
            assert str(exc)=='operation-not-ready'
        else:
            raise AssertionError('ambiguous operation replayed')
        block.apply_stale_cost_weeks.assert_called_once()


def main() -> None:
    finance_stale_adapter_contract()
    assert set(ADAPTERS) == {"finance_stale_cost_v1", "wb_autoanswers_recovery_v1", "inventory_retention_publication_v1", "web_source_publication_v1", "promo_archive_publication_v1", "finance_daily_publication_v1", "ads_partial_publication_v1", "ads_closed_day_recovery_v1", "fbs_snapshot_accounting_v1", "finance_payout_reconcile_v1", "wb_fbs_mapping_evidence_v1", "web_vitrina_management_history_v1", "web_vitrina_wb_history_recovery_v1", "supplier_invoice_revision_v1", "search_cluster_cleaner_manual_v1"}
    for adapter_name in ADAPTERS:
        registered = ADAPTERS[adapter_name]
        assert callable(registered.preview)
        assert callable(registered.apply)
        assert callable(registered.readback)
    assert isinstance(
        ADAPTERS["wb_fbs_mapping_evidence_v1"],
        WbFbsMappingEvidenceProductionAdapter,
    )
    assert REMOTE_APP == "/opt/wb-core-runtime/app/apps/wb_fbs_mapping_evidence.py"
    assert REMOTE_RUNTIME_DIR == "/opt/wb-core-runtime/state"
    assert REMOTE_ENV_FILE == "/opt/wb-ai/.env"
    request = {"mapping_id": "fixture"}
    harness = Harness()
    preview = harness.preview(request, "operation-fixture-0001")
    harness.readback(request, "operation-fixture-0001")
    harness.apply(request, "operation-fixture-0001", preview)
    assert [call["action"] for call in harness.calls] == [
        "preview",
        "readback",
        "apply",
    ]
    assert harness.calls[-1]["expected_prestate"] == preview["prestate_sha256"]
    assert harness.calls[-1]["expected_candidate"] == preview["candidate_sha256"]
    _assert_transport_envelope_and_ambiguity()
    print("production apply adapters smoke: ok")


def _assert_transport_envelope_and_ambiguity() -> None:
    calls = []
    adapter_module.configure_ssh = lambda _path: None
    adapter_module.trusted_main_sha = lambda: "a" * 40
    os.environ["WB_CORE_HOSTED_RUNTIME_SSH_IDENTITY_FILE"] = "/tmp/fixture-key"
    os.environ["WB_CORE_HOSTED_RUNTIME_SSH_OPTIONS"] = ""

    def success(command, **kwargs):
        calls.append((command, kwargs))
        envelope = json.loads(kwargs["input"])
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "operation_id": envelope["operation_id"],
                    "target": "fixture",
                    "scope": {"row_count": 1},
                    "prestate_sha256": "sha256:" + "1" * 64,
                    "candidate_sha256": "sha256:" + "2" * 64,
                    "recovery": {"kind": "append_only_reversion"},
                }
            ),
            stderr="",
        )

    adapter_module.subprocess.run = success
    adapter = WbFbsMappingEvidenceProductionAdapter()
    request = {"mapping_id": "fixture"}
    adapter.preview(request, "operation-fixture-transport")
    command, kwargs = calls[-1]
    envelope = json.loads(kwargs["input"])
    assert command[-6:] == [
        "python3",
        REMOTE_APP,
        "--runtime-dir",
        REMOTE_RUNTIME_DIR,
        "--env-file",
        REMOTE_ENV_FILE,
    ]
    assert envelope["action"] == "preview"
    assert envelope["expected_runtime_sha"] == "a" * 40
    assert envelope["request"] == request
    assert kwargs["timeout"] == 150 and kwargs["check"] is False

    def uncertain(_command, **_kwargs):
        return SimpleNamespace(
            returncode=17,
            stdout=json.dumps(
                {
                    "operation_id": "operation-fixture-transport",
                    "disposition": "submitted",
                }
            ),
            stderr="transport ended after output",
        )

    adapter_module.subprocess.run = uncertain
    try:
        adapter.apply(
            request,
            "operation-fixture-transport",
            {
                "prestate_sha256": "sha256:" + "1" * 64,
                "candidate_sha256": "sha256:" + "2" * 64,
            },
        )
    except AmbiguousSubmit:
        pass
    else:
        raise AssertionError("uncertain apply transport was accepted as deterministic")

    def blocked(_command, **_kwargs):
        return SimpleNamespace(
            returncode=2,
            stdout=json.dumps(
                {
                    "status": "blocked",
                    "error": {"code": "exact-precondition-blocked"},
                }
            ),
            stderr="",
        )

    adapter_module.subprocess.run = blocked
    try:
        adapter.apply(
            request,
            "operation-fixture-transport",
            {
                "prestate_sha256": "sha256:" + "1" * 64,
                "candidate_sha256": "sha256:" + "2" * 64,
            },
        )
    except AdapterError as exc:
        assert str(exc) == "exact-precondition-blocked"
    else:
        raise AssertionError("explicit remote precondition block was ambiguous")

    def timeout(_command, **_kwargs):
        raise subprocess.TimeoutExpired("ssh", 150)

    adapter_module.subprocess.run = timeout
    try:
        adapter.apply(
            request,
            "operation-fixture-transport",
            {
                "prestate_sha256": "sha256:" + "1" * 64,
                "candidate_sha256": "sha256:" + "2" * 64,
            },
        )
    except AmbiguousSubmit:
        pass
    else:
        raise AssertionError("apply timeout did not require readback-only handling")


if __name__ == "__main__":
    main()
