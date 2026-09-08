#!/usr/bin/env python3
"""Small deterministic smoke for the check selector."""

from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from unittest.mock import patch

import select_checks
from select_checks import PlanError, build_plan_from_paths, verify_plan


BASE = "1" * 40
HEAD = "2" * 40

# Independent expected commands: a package path has no automatic apps/ sibling.
# Keep these assertions when splitting/renaming a selected production boundary.
BOUNDARIES = {
    "warehouse_ff_acceptance_form_smoke": (
        "packages/application/supplier_shipments.py",
        "packages/application/registry_upload_db_backed_runtime.py",
        "packages/application/registry_upload_http_entrypoint.py",
    ),
    "sheet_vitrina_v1_web_vitrina_historical_completion_smoke": (
        "packages/application/web_vitrina_historical_ready_snapshot_import.py",
        "packages/application/registry_upload_db_backed_runtime.py",
    ),
    "warehouse_current_sync_job_smoke": (
        "apps/warehouse_functional_runner.py",
        "packages/application/warehouse_update_journal.py",
        "packages/application/warehouse_functional_lock.py",
        "packages/application/warehouse_sync_lock.py",
        "packages/application/registry_upload_http_entrypoint.py",
        "packages/adapters/registry_upload_http_entrypoint.py",
        "packages/adapters/templates/sheet_vitrina_v1_web_vitrina.html",
    ),
    "warehouse_functional_smoke": (
        "apps/warehouse_functional_runner.py",
        "packages/application/warehouse_functional.py",
        "packages/application/warehouse_functional_lock.py",
        "packages/application/warehouse_sync_lock.py",
        "packages/application/calculation_parameters.py",
        "packages/application/warehouse_functional_economics_backfill.py",
        "packages/application/warehouse_targeted_replay.py",
        "packages/application/supplier_shipment_factual_correction.py",
    ),
    "sheet_vitrina_v1_ready_snapshot_runtime_smoke": (
        "packages/application/registry_upload_db_backed_runtime.py",
        "packages/application/registry_upload_http_entrypoint.py",
        "packages/adapters/registry_upload_http_entrypoint.py",
        "packages/application/fbs_accounting_runtime.py",
        "packages/application/web_vitrina_historical_ready_snapshot_import.py",
    ),
    "fbs_accounting_apply_smoke": (
        "apps/fbs_accounting_runtime.py",
        "packages/application/fbs_accounting_runtime.py",
        "packages/application/fbs_accounting_apply.py",
        "packages/application/fbs_snapshot_cost.py",
        "packages/application/calculation_parameters.py",
        "packages/application/registry_upload_db_backed_runtime.py",
    ),
    "fbs_snapshot_cost_sources_smoke": (
        "packages/application/fbs_snapshot_cost_sources.py",
        "packages/application/shared_sku_cost_sources.py",
        "packages/application/fbs_inventory_presentation.py",
        "packages/application/fbs_accounting_runtime.py",
        "packages/application/fbs_accounting_apply.py",
    ),
    "web_vitrina_daily_pool_smoke": (
        "packages/application/vitrina_catalog.py",
        "packages/application/vitrina_economics.py",
        "packages/application/daily_trading_pool.py",
        "packages/application/web_vitrina_management_history.py",
        "packages/application/sheet_vitrina_v1_web_vitrina.py",
        "packages/application/registry_upload_db_backed_runtime.py",
        "packages/application/registry_upload_http_entrypoint.py",
        "packages/application/fbs_accounting_runtime.py",
        "apps/web_vitrina_management_history.py",
    ),
    "sheet_vitrina_v1_supplier_shipments_http_smoke": (
        "packages/application/supplier_shipments.py",
        "packages/application/registry_upload_db_backed_runtime.py",
        "packages/application/registry_upload_http_entrypoint.py",
    ),
    "supplier_financial_documents_smoke": (
        "packages/application/supplier_financial_documents.py",
        "packages/application/registry_upload_db_backed_runtime.py",
    ),
    "supplier_invoice_revision_smoke": (
        "packages/application/supplier_shipment_invoice_revision.py",
    ),
    "cny_ledger_smoke": (
        "packages/application/cny_ledger.py",
        "packages/application/registry_upload_db_backed_runtime.py",
    ),
    "sheet_vitrina_v1_fulfillment_services_smoke": (
        "packages/application/fulfillment_services.py",
        "packages/application/registry_upload_http_entrypoint.py",
        "packages/adapters/registry_upload_http_entrypoint.py",
    ),
    "ff_inventory_reconciliation_smoke": (
        "packages/application/ff_stock_ledger.py",
        "packages/application/ff_inventory_reconciliation.py",
        "packages/application/ff_overhead_allocation.py",
        "packages/application/ff_document_workflow.py",
        "packages/application/warehouse_business_projection.py",
        "packages/application/registry_upload_db_backed_runtime.py",
    ),
    "ff_pool_dense_fbs_smoke": (
        "packages/application/ff_pool_documents.py",
        "packages/application/ff_pool_dense_fbs.py",
        "packages/application/registry_upload_db_backed_runtime.py",
    ),
    "warehouse_historical_recovery_smoke": (
        "packages/application/warehouse_historical_recovery.py",
        "packages/application/warehouse_fbs_material_rematerialization.py",
    ),
    "finance_daily_historical_recovery_smoke": (
        "packages/application/finance_daily_historical_recovery.py",
        "packages/application/registry_upload_db_backed_runtime.py",
    ),
}

# Direct writers retain explicit checks even when a renamed old file disappears.
DIRECT_WRITERS = (
    "canonical_cost_engine_vitrina_publication",
    "sheet_vitrina_v1_proxy_v4_initialize",
    "sheet_vitrina_v1_proxy_v4_reconcile",
    "sheet_vitrina_v1_proxy_v4_transit_repair",
    "sheet_vitrina_v1_historical_cost_carry_forward",
    "web_vitrina_management_history",
    "promo_metric_eligibility_recompute",
    "spp_metric_recompute",
    "sheet_vitrina_v1_proxy_margin_3_historical_backfill",
    "supplier_shipment_publication_chain",
)


def boundary_checks():
    root = select_checks.ROOT
    expected = {}
    for smoke, paths in BOUNDARIES.items():
        for path in paths:
            expected.setdefault(path, set()).add(("python3", f"apps/{smoke}.py"))
    for name in DIRECT_WRITERS:
        smoke = "sheet_vitrina_v1_proxy_v4_initialize" if name == "sheet_vitrina_v1_proxy_v4_reconcile" else name
        expected.setdefault(f"apps/{name}.py", set()).add(("python3", f"apps/{smoke}_smoke.py"))
    for path, required in expected.items():
        assert (root / path).is_file(), path
        for command in required:
            assert (root / command[1]).is_file(), command
        plan = build_plan_from_paths(pull_request=20, base=BASE, head=HEAD,
            paths=[path], file_exists=lambda _, p: (root / p).is_file())
        verify_plan(plan)
        assert required <= {tuple(c) for c in plan["commands"]}, (path, required, plan)
        # A rename must use the old path from the diff even after it is absent
        # from candidate HEAD. Do not mistake this for coverage of later edits
        # to an arbitrary new name: that PR must update its map or sibling.
        renamed = str(Path(path).with_name("renamed_" + Path(path).name))
        renamed_smoke = renamed[:-3] + "_smoke.py"
        rename = build_plan_from_paths(pull_request=21, base=BASE, head=HEAD,
            paths=[path, renamed], file_exists=lambda _, p: p in {renamed, renamed_smoke} or (p != path and (root / p).is_file()))
        commands = {tuple(c) for c in rename["commands"]}
        assert required <= commands, (path, rename)
        assert path not in rename["commands"][0][3:], "deleted old path was compiled"

    for paths in (["docs/example.md"], ["packages/application/wb_autoanswers_runtime.py"],
                  ["docs/example.md", "packages/application/wb_autoanswers_runtime.py"]):
        plan = build_plan_from_paths(pull_request=22, base=BASE, head=HEAD,
            paths=paths, file_exists=lambda *_: True)
        commands = {tuple(c) for c in plan["commands"]}
        assert not commands.intersection(set().union(*expected.values())), plan
        assert len(plan["groups"]) <= 1, plan

    helper = build_plan_from_paths(pull_request=23, base=BASE, head=HEAD,
        paths=["ci/fixture_process.py"], file_exists=lambda _, p: (root / p).is_file())
    assert helper["release_kind"] == "repo_only"
    assert ["python3", "apps/warehouse_process_fixture_smoke.py"] in helper["commands"]
    process = build_plan_from_paths(pull_request=25, base=BASE, head=HEAD,
        paths=["ci/checks.json"], file_exists=lambda _, p: (root / p).is_file())
    assert process["groups"] == ["process"]
    assert process["pip"] == ["openpyxl==3.1.5"], process
    new_helper = "packages/application/example_publication_helper.py"
    sibling = new_helper[:-3] + "_smoke.py"
    plan = build_plan_from_paths(pull_request=24, base=BASE, head=HEAD,
        paths=[new_helper], file_exists=lambda _, p: p in {new_helper, sibling})
    assert plan["commands"] == [["python3", "-m", "py_compile", new_helper], ["python3", sibling]]
    print(f"boundary selection: {len(expected)} production paths and renames; unrelated/helper routes OK")


def rename_diff_check():
    # Exercise the actual Git name-status parser in a disposable repository.
    with TemporaryDirectory(prefix="selector-rename-") as raw:
        root = Path(raw)
        def git(*args):
            return subprocess.check_output(["git", "-c", "user.name=Fixture", "-c",
                "user.email=fixture@example.invalid", *args], cwd=root, text=True).strip()
        git("init", "-q")
        (root / "apps").mkdir()
        original = "apps/fbs_accounting_runtime.py"
        renamed = "apps/accounting_renamed.py"
        (root / original).write_text("# fixture rename only\n", encoding="utf-8")
        git("add", original)
        git("commit", "-qm", "fixture before")
        base = git("rev-parse", "HEAD")
        git("mv", original, renamed)
        git("commit", "-qm", "fixture after")
        head = git("rev-parse", "HEAD")
        with patch.object(select_checks, "ROOT", root):
            paths = select_checks.changed_paths(base, head)
        assert paths == sorted([original, renamed]), paths


def exists(_head: str, path: str) -> bool:
    return path in {
        "docs/example.md",
        "packages/application/finance_value.py",
        "packages/application/finance_value_smoke.py",
        "packages/application/web_vitrina_value.py",
        "packages/application/warehouse_value.py",
        "apps/example.py",
        "apps/example_smoke.py",
        "unknown.bin",
    }


def main() -> None:
    # The hosted system Python may install into user-site, which -I correctly
    # excludes. Dependency install, trusted harness and nested Python must share
    # the same ephemeral venv; the existing launcher smoke exercises -I for real.
    workflow = (select_checks.ROOT / ".github/workflows/pr-gate.yml").read_text()
    assert 'check_venv="$RUNNER_TEMP/wb-core-checks-venv"' in workflow
    assert 'python3 -m venv "$check_venv"' in workflow
    assert '"$check_venv/bin/python" -m pip install --disable-pip-version-check $packages' in workflow
    assert 'echo "$check_venv/bin" >> "$GITHUB_PATH"' in workflow
    assert workflow.index('>> "$GITHUB_PATH"') < workflow.index('python3 trusted-base/ci/run_checks.py')
    boundary_checks()
    rename_diff_check()
    docs = build_plan_from_paths(
        pull_request=1, base=BASE, head=HEAD, paths=["docs/example.md"], file_exists=exists
    )
    verify_plan(docs)
    assert docs["release_kind"] == "repo_only"
    assert docs["commands"] == []

    timer = build_plan_from_paths(
        pull_request=10, base=BASE, head=HEAD,
        paths=["artifacts/registry_upload_http_entrypoint/systemd/wb-core-fbs-shadow-collector.timer"],
        file_exists=lambda *_: True,
    )
    verify_plan(timer)
    assert timer["release_kind"] == "live_runtime"

    finance = build_plan_from_paths(
        pull_request=2,
        base=BASE,
        head=HEAD,
        paths=["packages/application/finance_value.py"],
        file_exists=exists,
    )
    verify_plan(finance)
    assert finance["release_kind"] == "live_runtime"
    assert "finance" in finance["groups"]
    assert ["python3", "apps/wb_finance_weekly_smoke.py"] in finance["commands"]
    assert ["python3", "packages/application/finance_value_smoke.py"] in finance["commands"]

    web_vitrina = build_plan_from_paths(
        pull_request=3,
        base=BASE,
        head=HEAD,
        paths=["packages/application/web_vitrina_value.py"],
        file_exists=exists,
    )
    verify_plan(web_vitrina)
    assert "web_vitrina" in web_vitrina["groups"]
    assert "openpyxl==3.1.5" in web_vitrina["pip"]

    warehouse = build_plan_from_paths(
        pull_request=4,
        base=BASE,
        head=HEAD,
        paths=["packages/application/warehouse_value.py"],
        file_exists=exists,
    )
    verify_plan(warehouse)
    assert "warehouse" in warehouse["groups"]
    assert "openpyxl==3.1.5" in warehouse["pip"]

    promo = build_plan_from_paths(
        pull_request=9, base=BASE, head=HEAD,
        paths=["packages/application/promo_live_source.py"], file_exists=lambda *_: True,
    )
    verify_plan(promo)
    assert "openpyxl==3.1.5" in promo["pip"]
    assert ["python3", "apps/promo_xlsx_collector_contract_smoke.py"] in promo["commands"]
    assert ["python3", "apps/sheet_vitrina_v1_promo_live_source_smoke.py"] in promo["commands"]

    browser = build_plan_from_paths(
        pull_request=7, base=BASE, head=HEAD,
        paths=["packages/adapters/templates/sheet_vitrina_v1_web_vitrina.html"],
        file_exists=lambda *_: True,
    )
    verify_plan(browser)
    install = ["python3", "-m", "playwright", "install", "--with-deps", "chromium"]
    smoke = ["python3", "apps/sku_inventory_balance_browser_smoke.py"]
    assert browser["commands"].index(install) < browser["commands"].index(smoke)
    assert "playwright==1.58.0" in browser["pip"]
    assert "openpyxl==3.1.5" in browser["pip"]
    backend = build_plan_from_paths(
        pull_request=8, base=BASE, head=HEAD,
        paths=["packages/application/sku_inventory_balance.py", "packages/application/change_registry_writer.py"],
        file_exists=lambda *_: True,
    )
    verify_plan(backend)
    assert "inventory_balance" in backend["groups"]
    assert "change_registry_writer" in backend["groups"]
    assert "openpyxl==3.1.5" in backend["pip"]
    assert install not in backend["commands"]
    assert "playwright==1.58.0" not in finance["pip"]
    assert install not in docs["commands"]

    try:
        build_plan_from_paths(
            pull_request=5, base=BASE, head=HEAD, paths=["unknown.bin"], file_exists=exists
        )
    except PlanError:
        pass
    else:
        raise AssertionError("unknown path was accepted")

    deleted_history = build_plan_from_paths(
        pull_request=6,
        base=BASE,
        head=HEAD,
        paths=["migration/old-note.md"],
        file_exists=exists,
    )
    verify_plan(deleted_history)
    assert deleted_history["release_kind"] == "repo_only"
    print("select_checks_smoke: ok")


if __name__ == "__main__":
    main()
