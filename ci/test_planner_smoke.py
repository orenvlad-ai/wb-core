#!/usr/bin/env python3
"""Deterministic smoke coverage for registry union and impact selection."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ci import replay_test_selection
from ci import test_planner as planner


BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40


def registry() -> dict:
    return {
        "schema": planner.REGISTRY_SCHEMA,
        "protocol": {
            "version": 2,
            "cutover_epoch": "4f0333ad7b500967fe4175aa6e53359043832360",
            "full_regression_suites": ["core", "finance", "warehouse"],
        },
        "suites": {
            "core": {"group": "core", "depends_on": [], "commands": [["python3", "core.py"]]},
            "warehouse": {"group": "domain", "depends_on": ["core"], "commands": [["python3", "warehouse.py"]]},
            "finance": {"group": "domain", "depends_on": ["warehouse"], "commands": [["python3", "finance.py"]]},
        },
        "rules": [
            {"id": "core", "paths": ["ci/**"], "suites": ["core"], "release": "repo_only", "force_full": True},
            {"id": "finance", "paths": ["apps/finance_*"], "suites": ["finance"], "release": "live_runtime"},
        ],
    }


def build(base: dict | None, head: dict, changes: list[dict[str, str]]) -> dict:
    return planner.build_plan(
        pr_number=1041,
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
        base_registry=base,
        head_registry=head,
        changes=changes,
    )


def changed(*paths: str, status: str = "M") -> list[dict[str, str]]:
    return [{"status": status, "path": path} for path in paths]


def executed_commands(plan: dict) -> list[tuple[str, ...]]:
    return [
        tuple(command)
        for suite in plan["execution"].values()
        for command in suite["commands"]
    ]


def _run(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        check=True,
        text=True,
        capture_output=True,
    )


def _fixture_planner(label: str) -> str:
    return f'''#!/usr/bin/env python3
import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--base", required=True)
parser.add_argument("--head", required=True)
parser.add_argument("--output", required=True, type=Path)
args = parser.parse_args()
args.output.write_text(
    json.dumps(
        {{"base": args.base, "head": args.head, "planner_semantics": "{label}"}},
        separators=(",", ":"),
        sort_keys=True,
    ),
    encoding="utf-8",
)
'''


def _fixture_group_harness(label: str) -> str:
    write_marker = (
        'args.marker.write_text(Path.cwd().name, encoding="utf-8")'
        if label == "base"
        else "pass"
    )
    return f'''#!/usr/bin/env python3
import argparse
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--marker", required=True, type=Path)
args = parser.parse_args()
{write_marker}
'''


def candidate_group_harness_smoke() -> None:
    candidate_registry = registry()
    for suite_id, suite in candidate_registry["suites"].items():
        suite["commands"] = [
            [sys.executable, "-c", f"assert {suite_id!r} == {suite_id!r}"]
        ]
    plan = build(
        candidate_registry,
        candidate_registry,
        changed("ci/run_test_group.py"),
    )
    with tempfile.TemporaryDirectory(prefix="wb-core-candidate-harness-") as raw:
        plan_path = Path(raw) / "test-plan.json"
        plan_path.write_bytes(planner.canonical_json_bytes(plan) + b"\n")
        isolated_env = os.environ.copy()
        isolated_env.pop("PYTHONHOME", None)
        isolated_env.pop("PYTHONPATH", None)
        subprocess.run(
            [
                sys.executable,
                "-I",
                "ci/run_test_group.py",
                "--plan",
                str(plan_path),
                "--group",
                "core",
            ],
            cwd=ROOT,
            env=isolated_env,
            check=True,
            capture_output=True,
        )


def trusted_base_materialization_smoke() -> None:
    """Changed head planner bytes cannot replace exact-base plan semantics."""

    with tempfile.TemporaryDirectory(prefix="wb-core-trusted-base-smoke-") as raw:
        root = Path(raw)
        remote = root / "remote.git"
        seed = root / "seed"
        trusted = root / "trusted"
        _run(["git", "init", "--bare", str(remote)], root)
        _run(["git", "init", str(seed)], root)
        _run(["git", "config", "user.email", "planner-smoke@example.invalid"], seed)
        _run(["git", "config", "user.name", "Planner Smoke"], seed)
        (seed / "ci").mkdir()
        (seed / "ci/test_planner.py").write_text(
            _fixture_planner("base"), encoding="utf-8"
        )
        (seed / "ci/run_test_group.py").write_text(
            _fixture_group_harness("base"), encoding="utf-8"
        )
        _run(["git", "add", "ci/test_planner.py", "ci/run_test_group.py"], seed)
        _run(["git", "commit", "-m", "base planner"], seed)
        base = _run(["git", "rev-parse", "HEAD"], seed).stdout.strip()
        _run(["git", "remote", "add", "origin", str(remote)], seed)
        _run(["git", "push", "origin", "HEAD:refs/heads/main"], seed)
        _run(
            ["git", "--git-dir", str(remote), "symbolic-ref", "HEAD", "refs/heads/main"],
            root,
        )

        (seed / "ci/test_planner.py").write_text(
            _fixture_planner("head"), encoding="utf-8"
        )
        (seed / "ci/run_test_group.py").write_text(
            _fixture_group_harness("head"), encoding="utf-8"
        )
        _run(["git", "add", "ci/test_planner.py", "ci/run_test_group.py"], seed)
        _run(["git", "commit", "-m", "changed head planner"], seed)
        head = _run(["git", "rev-parse", "HEAD"], seed).stdout.strip()
        _run(["git", "push", "origin", "HEAD:refs/pull/1041/head"], seed)

        _run(["git", "clone", "--no-checkout", str(remote), str(trusted)], root)
        _run(["git", "checkout", "--detach", base], trusted)
        assert _run(["git", "rev-parse", "HEAD"], trusted).stdout.strip() == base
        _run(
            [
                "git",
                "fetch",
                "--no-tags",
                "--no-recurse-submodules",
                "origin",
                "+refs/pull/1041/head:refs/remotes/origin/pr-plan-head",
            ],
            trusted,
        )
        resolved = _run(
            ["git", "rev-parse", "refs/remotes/origin/pr-plan-head^{commit}"],
            trusted,
        ).stdout.strip()
        assert resolved == head
        assert _run(["git", "rev-parse", "HEAD"], trusted).stdout.strip() == base

        trusted_output = root / "trusted-plan.json"
        candidate_output = root / "candidate-plan.json"
        isolated_env = os.environ.copy()
        isolated_env.pop("PYTHONHOME", None)
        isolated_env.pop("PYTHONPATH", None)
        subprocess.run(
            [
                sys.executable,
                "-I",
                "ci/test_planner.py",
                "--base",
                base,
                "--head",
                head,
                "--output",
                str(trusted_output),
            ],
            cwd=trusted,
            env=isolated_env,
            check=True,
        )
        subprocess.run(
            [
                sys.executable,
                "-I",
                "ci/test_planner.py",
                "--base",
                base,
                "--head",
                head,
                "--output",
                str(candidate_output),
            ],
            cwd=seed,
            env=isolated_env,
            check=True,
        )
        trusted_plan = json.loads(trusted_output.read_text(encoding="utf-8"))
        candidate_plan = json.loads(candidate_output.read_text(encoding="utf-8"))
        assert trusted_plan["planner_semantics"] == "base"
        assert candidate_plan["planner_semantics"] == "head"
        assert trusted_output.read_bytes() != candidate_output.read_bytes()

        trusted_marker = root / "trusted-harness.marker"
        candidate_marker = root / "candidate-harness.marker"
        subprocess.run(
            [
                sys.executable,
                "-I",
                str(trusted / "ci/run_test_group.py"),
                "--marker",
                str(trusted_marker),
            ],
            cwd=seed,
            env=isolated_env,
            check=True,
        )
        subprocess.run(
            [
                sys.executable,
                "-I",
                "ci/run_test_group.py",
                "--marker",
                str(candidate_marker),
            ],
            cwd=seed,
            env=isolated_env,
            check=True,
        )
        assert trusted_marker.read_text(encoding="utf-8") == "seed"
        assert not candidate_marker.exists()


def main() -> None:
    base = registry()
    head = copy.deepcopy(base)
    head["suites"]["core"]["commands"] = [["python3", "new_core.py"]]
    union, reasons = planner.union_registries(base, head)
    assert reasons == []
    assert union["suites"]["core"]["commands"] == [
        ["python3", "core.py"],
        ["python3", "new_core.py"],
    ]

    finance = build(base, head, [{"status": "M", "path": "apps/finance_storage.py"}])
    planner.verify_plan(finance)
    assert finance["selected_suites"] == ["core", "finance", "warehouse"]
    assert finance["browser_groups"] == []
    assert finance["release_plan"]["kind"] == "live_runtime"
    assert "transitive-domain-dependency" in finance["reason_codes"]

    unknown = build(base, head, [{"status": "A", "path": "new-domain/file.txt"}])
    planner.verify_plan(unknown)
    assert unknown["selected_suites"] == ["core", "finance", "warehouse"]
    assert unknown["unknown_paths"] == ["new-domain/file.txt"]
    assert unknown["release_plan"]["kind"] == "live_runtime"
    assert "unknown-path-full-regression" in unknown["reason_codes"]

    core = build(base, head, [{"status": "M", "path": "ci/test_registry.json"}])
    assert core["selected_suites"] == ["core", "finance", "warehouse"]
    encoded = planner.canonical_json_bytes(core)
    reordered = json.loads(json.dumps(core, sort_keys=False))
    assert encoded == planner.canonical_json_bytes(reordered)
    planner.verify_plan(reordered)

    duplicate = copy.deepcopy(head)
    duplicate["suites"]["warehouse"]["commands"].append(["python3", "new_core.py"])
    duplicate_plan = build(
        duplicate,
        duplicate,
        [{"status": "A", "path": "unknown/new-code.py"}],
    )
    assert executed_commands(duplicate_plan).count(("python3", "new_core.py")) == 1
    assert "duplicate-command-deduplicated" in duplicate_plan["reason_codes"]

    missing_base = build(None, head, [{"status": "M", "path": "apps/finance_storage.py"}])
    assert "base-registry-missing" in missing_base["reason_codes"]

    cutover = planner.build_plan(
        pr_number=1041,
        base_sha=head["protocol"]["cutover_epoch"],
        head_sha=HEAD_SHA,
        base_registry=None,
        head_registry=head,
        changes=[{"status": "M", "path": "apps/finance_storage.py"}],
    )
    assert cutover["release_plan"]["kind"] == "repo_only"
    assert cutover["release_plan"]["deploy_required"] is False
    assert "cutover-bootstrap-no-deploy" in cutover["reason_codes"]

    real_registry = json.loads((ROOT / planner.REGISTRY_PATH).read_text(encoding="utf-8"))
    planner.validate_registry(real_registry, "repository registry")
    coverage = planner.registered_command_coverage(real_registry)
    assert coverage["valid"] is True
    assert coverage["gaps"] == []
    assert coverage["registered_command_count"] >= 100
    assert coverage["core_only_command_count"] == 3
    real_plan = planner.build_plan(
        pr_number=1041,
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
        base_registry=real_registry,
        head_registry=real_registry,
        changes=[{"status": "M", "path": "apps/finance_storage.py"}],
    )
    assert "finance" in real_plan["browser_groups"]

    full = set(real_registry["protocol"]["full_regression_suites"])
    orchestration_paths = (
        "AGENTS.md",
        "docs/architecture/07_codex_execution_protocol.md",
        "docs/architecture/12_codex_global_orchestration.md",
        "docs/architecture/13_codex_curator_workspace.md",
        ".codex/config.toml",
        ".github/pull_request_template.md",
    )
    for path in orchestration_paths:
        plan = build(real_registry, real_registry, changed(path))
        assert plan["selected_suites"] == ["release_safety"], path
        assert plan["release_plan"]["kind"] == "repo_only", path

    release_tooling_paths = (
        "apps/github_release_runner.py",
        "apps/github_release_runner_smoke.py",
        "apps/production_apply_runner.py",
        "apps/production_apply_runner_smoke.py",
        "apps/release_protocol.py",
        ".github/workflows/release-runner.yml",
        ".github/workflows/production-apply.yml",
        "docs/architecture/11_github_release_train.md",
    )
    for path in release_tooling_paths:
        plan = build(real_registry, real_registry, changed(path))
        assert plan["selected_suites"] == ["release_safety"], path
        assert plan["release_plan"]["kind"] == "repo_only", path

    for path in (
        "ci/test_registry.json",
        "ci/test_planner.py",
        "ci/run_test_group.py",
        ".github/workflows/pr-gate.yml",
        "apps/registry_upload_smoke_support.py",
    ):
        plan = build(real_registry, real_registry, changed(path))
        assert set(plan["selected_suites"]) == full, path
        assert plan["release_plan"]["kind"] == "repo_only", path

    history_commands = {
        ("python3", "apps/inventory_planning_read_model_smoke.py"),
        ("python3", "apps/sheet_vitrina_v1_inventory_history_smoke.py"),
        ("python3", "apps/sheet_vitrina_v1_inventory_history_backfill_smoke.py"),
        ("python3", "apps/sheet_vitrina_v1_inventory_planning_smoke.py"),
    }
    for path in (
        "packages/application/sheet_vitrina_v1_inventory_history.py",
        "apps/sheet_vitrina_v1_inventory_history_smoke.py",
        "apps/sheet_vitrina_v1_inventory_history_backfill_smoke.py",
    ):
        plan = build(real_registry, real_registry, changed(path))
        assert set(plan["selected_suites"]) == {"inventory_history", "release_safety"}, path
        assert history_commands <= set(executed_commands(plan)), path
        assert "finance_storage" not in plan["selected_suites"], path

    planning = build(
        real_registry,
        real_registry,
        changed("packages/application/sheet_vitrina_v1_inventory_planning.py"),
    )
    assert set(planning["selected_suites"]) == {
        "inventory_history",
        "inventory_history_browser",
        "release_safety",
    }
    assert ("python3", "apps/sheet_vitrina_v1_inventory_planning_browser_smoke.py") in set(
        executed_commands(planning)
    )
    assert "history-browser" in planning["browser_groups"]
    assert "finance_storage" not in planning["selected_suites"]

    business = build(
        real_registry,
        real_registry,
        changed("apps/business_data_maintenance_smoke.py"),
    )
    assert "business_data_safety" in business["selected_suites"]
    assert "finance_storage" not in business["selected_suites"]
    assert ("python3", "apps/business_data_maintenance_smoke.py") in set(
        executed_commands(business)
    )

    unknown_code = build(
        real_registry,
        real_registry,
        changed("packages/new_domain/unregistered.py", status="A"),
    )
    assert set(unknown_code["selected_suites"]) == full
    assert unknown_code["release_plan"]["kind"] == "live_runtime"
    assert "unknown-path-full-regression" in unknown_code["reason_codes"]

    deleted = build(
        real_registry,
        real_registry,
        changed("apps/sheet_vitrina_v1_inventory_history_smoke.py", status="D"),
    )
    assert "inventory_history" in deleted["selected_suites"]
    renamed = build(
        real_registry,
        real_registry,
        [
            {
                "status": "R100",
                "old_path": "apps/sheet_vitrina_v1_inventory_history_smoke.py",
                "path": "packages/new_domain/renamed_history_smoke.py",
            }
        ],
    )
    assert set(renamed["selected_suites"]) == full
    assert renamed["unknown_paths"] == ["packages/new_domain/renamed_history_smoke.py"]

    invalid = copy.deepcopy(base)
    invalid["suites"]["finance"]["depends_on"] = ["missing-suite"]
    invalid_plan = build(base, invalid, changed("apps/finance_storage.py"))
    assert set(invalid_plan["selected_suites"]) == set(base["protocol"]["full_regression_suites"])
    assert invalid_plan["registry"]["valid"] is False
    assert invalid_plan["release_plan"]["valid"] is False
    assert "head-registry-invalid-full-regression" in invalid_plan["reason_codes"]
    with tempfile.TemporaryDirectory(prefix="wb-core-planner-smoke-") as directory:
        output = Path(directory) / "github-output"
        planner.write_github_output(output, invalid_plan)
        values = dict(
            line.split("=", 1)
            for line in output.read_text(encoding="utf-8").splitlines()
        )
    assert values["execution_valid"] == "true"
    assert values["plan_valid"] == "false"
    assert values["release_valid"] == "false"

    incompatible_schema = copy.deepcopy(head)
    incompatible_schema["schema"] = "wb-core.test-registry/v3"
    staged = build(base, incompatible_schema, changed("ci/test_registry.json"))
    assert staged["registry"]["valid"] is False
    assert staged["release_plan"]["valid"] is False
    assert "head-registry-schema-incompatible-staged-migration" in staged["reason_codes"]

    removed_rule = copy.deepcopy(base)
    removed_rule["rules"] = [rule for rule in removed_rule["rules"] if rule["id"] != "finance"]
    preserved = build(base, removed_rule, changed("apps/finance_storage.py"))
    assert "finance" in preserved["selected_suites"]
    assert preserved["release_plan"]["kind"] == "live_runtime"

    current_runtime_mappings = {
        "apps/hosted_runtime_transport_reconcile.py": "release_safety",
        "apps/supplier_cost_status_smoke.py": "fulfillment",
        "packages/application/wb_finance_weekly.py": "finance_storage",
        "packages/adapters/wb_fbs_orders.py": "fulfillment",
        "packages/application/inventory_cost_blend.py": "web_vitrina",
        "packages/application/calculation_parameters_v4.py": "web_vitrina",
        "packages/application/russian_payment_orders.py": "fulfillment",
        "packages/application/canonical_rub_money.py": "fulfillment",
        "packages/adapters/stocks_block.py": "web_vitrina",
        "packages/application/simple_xlsx.py": "fulfillment",
        "packages/application/registry_upload_db_backed_runtime.py": "web_vitrina",
    }
    for path, expected_suite in current_runtime_mappings.items():
        mapped = build(real_registry, real_registry, changed(path))
        assert mapped["unknown_paths"] == [], path
        assert expected_suite in mapped["selected_suites"], path
        assert set(mapped["selected_suites"]) != full, path
        assert mapped["release_plan"]["kind"] == "live_runtime", path

    tree_audit = replay_test_selection.current_tree_mapping_audit(
        real_registry, "origin/main"
    )
    assert tree_audit["scope"] == ["apps", "packages", "gas"]
    assert (
        tree_audit["specifically_mapped_path_count"]
        + tree_audit["unmapped_residual_count"]
        == tree_audit["tracked_path_count"]
    )
    assert (
        tree_audit["baseline_specifically_mapped_path_count"]
        + tree_audit["baseline_unmapped_residual_count"]
        == tree_audit["baseline_tracked_path_count"]
    )
    assert tree_audit["new_unmapped_residual_paths"] == []
    assert tree_audit["unmapped_residual_paths"]

    protocol_text = "\n".join(
        (ROOT / path).read_text(encoding="utf-8")
        for path in (
            "AGENTS.md",
            "docs/architecture/07_codex_execution_protocol.md",
            "docs/architecture/12_codex_global_orchestration.md",
            "docs/architecture/13_codex_curator_workspace.md",
            "docs/architecture/15_codex_authorization_router.md",
        )
    )
    assert "collaboration.spawn_agent" in protocol_text
    for forbidden_thread_tool in (
        "codex_app.create_thread",
        "fork_thread",
        "handoff_thread",
        "send_message_to_thread",
    ):
        assert forbidden_thread_tool in protocol_text
    assert "istoriya-ostatkov" in protocol_text
    compact_protocol_text = " ".join(protocol_text.split())
    assert (
        "active не более одного mutating/implementation subagent"
        in compact_protocol_text
    )
    assert (
        "zero-or-more независимых bounded diagnostic/read-only subagents"
        in compact_protocol_text
    )
    assert (
        "Один и тот же question параллельно не дублируется"
        in compact_protocol_text
    )
    assert (
        "immutable/exact snapshot boundary либо ждёт stable boundary"
        in compact_protocol_text
    )
    assert "покрывающий весь текущий active set subagents" in compact_protocol_text
    release_protocol_text = (
        ROOT / "docs/architecture/11_github_release_train.md"
    ).read_text(encoding="utf-8")
    assert "--target-file <canonical-target>" in release_protocol_text
    assert "legacy/default target" in release_protocol_text
    baseline_text = (ROOT / ".github/workflows/baseline-ci.yml").read_text(encoding="utf-8")
    assert "codex/process-cutover-pr-gate" not in baseline_text
    assert "codex/pr-gate-rollback-" in baseline_text
    assert not (ROOT / ".codex/config.toml").exists()
    core_only = {
        tuple(entry["command"])
        for entry in real_registry["protocol"]["core_only_commands"]
    }
    fast_core = planner.fast_core_workflow_commands()
    assert all(fast_core.count(command) == 1 for command in core_only)
    assert all((ROOT / planner._repo_command_path(command)).is_file() for command in core_only)
    pr_gate_text = (ROOT / ".github/workflows/pr-gate.yml").read_text(encoding="utf-8")
    assert "if: needs.plan.outputs.execution_valid == 'true'" in pr_gate_text
    assert '"PLAN_VALID": "true"' in pr_gate_text
    assert '"RELEASE_VALID": "true"' in pr_gate_text
    assert "Checkout trusted exact PR base" in pr_gate_text
    assert "Materialize exact head objects read-only" in pr_gate_text
    assert "ref: ${{ steps.meta.outputs.base }}" in pr_gate_text
    assert "+refs/pull/$pr/head:$fetched_ref" in pr_gate_text
    assert "env -u PYTHONHOME -u PYTHONPATH /usr/bin/python3 -I ci/test_planner.py" in pr_gate_text
    assert pr_gate_text.count(
        "git config --local --unset-all http.https://github.com/.extraheader"
    ) == 4
    assert pr_gate_text.count(
        "checkout credential remained before candidate execution"
    ) == 2
    assert 'git worktree add --detach "$trusted_harness" "$base"' in pr_gate_text
    assert (
        '"$RUNNER_TEMP/trusted-base-harness/ci/run_test_group.py"'
        in pr_gate_text
    )
    assert "trusted harness checkout retained a credential" in pr_gate_text
    candidate_group_harness_smoke()
    trusted_base_materialization_smoke()
    assert planner._record_paths(
        [{"status": "R100", "old_path": "русский путь/до.md", "path": "русский путь/после.md"}]
    ) == ["русский путь/до.md", "русский путь/после.md"]
    print("test_planner_smoke: ok")


if __name__ == "__main__":
    main()
