"""Regression coverage for the user-artifact XLSX execution contract."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.release_protocol import (  # noqa: E402
    ExecutionContour,
    github_closure_required,
)
from apps.user_artifact_xlsx import (  # noqa: E402
    TabularXlsxSpec,
    create_tabular_xlsx,
    verify_tabular_xlsx,
    xlsx_creation_plan,
)


def _assert_protocol_contract() -> None:
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    execution = (ROOT / "docs" / "architecture" / "07_codex_execution_protocol.md").read_text(
        encoding="utf-8"
    )
    release_runner = (ROOT / "docs" / "architecture" / "11_github_release_train.md").read_text(
        encoding="utf-8"
    )
    sources = (agents, execution)
    assert len(tuple(ExecutionContour)) == 5
    assert ExecutionContour.USER_ARTIFACT.value == "user-artifact"
    assert not github_closure_required(ExecutionContour.USER_ARTIFACT)
    assert github_closure_required(ExecutionContour.REPO_ONLY)
    assert not github_closure_required(ExecutionContour.READ_ONLY)
    for source in sources:
        for required in (
            "`user-artifact`",
            "не является `ДИАГНОСТИКОЙ`",
            "branch",
            "worktree",
            "load_workspace_dependencies",
            "CODEX_PRIMARY_RUNTIME_NODE",
            "CODEX_PRIMARY_RUNTIME_NODE_MODULES",
            "openpyxl",
            "xlsxwriter",
            "OOXML",
        ):
            assert required in source
        assert "Отсутствие `load_workspace_dependencies` само по себе не blocker" in source
    for required in (
        "CODEX_PRIMARY_RUNTIME_ROOT",
        "CODEX_PRIMARY_RUNTIME_NODE",
        "CODEX_PRIMARY_RUNTIME_NODE_MODULES",
        "CODEX_PRIMARY_RUNTIME_PYTHON",
    ):
        assert required in execution
    assert "user-artifact" in release_runner and "не входит" in release_runner
    release_sources = "\n".join(
        (
            (ROOT / "apps" / "github_release_runner.py").read_text(encoding="utf-8"),
            (ROOT / ".github" / "pull_request_template.md").read_text(encoding="utf-8"),
        )
    )
    assert "scope:user-artifact" not in release_sources


def _assert_canonical_source_acquisition_contract() -> None:
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    source_policy = (
        ROOT / "docs" / "architecture" / "03_source_of_truth_policy.md"
    ).read_text(encoding="utf-8")
    execution = (
        ROOT / "docs" / "architecture" / "07_codex_execution_protocol.md"
    ).read_text(encoding="utf-8")
    hosted_runtime = (
        ROOT / "docs" / "architecture" / "10_hosted_runtime_deploy_contract.md"
    ).read_text(encoding="utf-8")
    release_runner = (
        ROOT / "docs" / "architecture" / "11_github_release_train.md"
    ).read_text(encoding="utf-8")
    module_index = (ROOT / "docs" / "modules" / "00_INDEX__MODULES.md").read_text(
        encoding="utf-8"
    )
    mcp_module = (
        ROOT / "docs" / "modules" / "38_MODULE__WEBCORE_DATA_MCP_BLOCK.md"
    ).read_text(encoding="utf-8")

    prompt_provenance = (
        "Выбор инструментов и источников не является требованием пользователя "
        "и всегда перепроверяется по актуальному протоколу, если пользователь "
        "отдельно явно не зафиксировал обратное."
    )
    for source in (agents, source_policy, execution):
        assert prompt_provenance in source
        assert "не называет WebCore Data MCP" in source
        assert "штатный SSH" in source
        assert "query_only=ON" in source
        assert "server-owned" in source

    for source in (agents, source_policy, execution, hosted_runtime):
        lowered = source.casefold()
        assert ("архивн" in lowered or "archived" in lowered) and "mcp" in lowered
        assert "canonical" in lowered and "server-side" in lowered
        assert "blocker" in lowered
    assert "one-shot" in release_runner.casefold()

    assert "Canonical Production Read-Only Evidence Path" in hosted_runtime
    assert "actual standard SSH connectivity/read preflight" in hosted_runtime
    assert "exact SSH/store/document error" in hosted_runtime
    assert "archived-compatibility/read-only-data-gateway" in module_index
    assert 'status: "archived_compatibility_implementation_retained"' in mcp_module
    assert "not a normal prompt/source/acquisition path" in mcp_module


def _assert_artifact_tool_failure_falls_back_without_data_loss() -> None:
    synthetic = {
        "sheet_name": "Номенклатура",
        "headers": ["Артикул", "nmID", "Штрихкод"],
        "rows": [
            ["Тест A | Barcode: 001234 | nmID: 0042", "0042", "001234"],
            ["Тест B | Barcode: 9000000000007 | nmID: 100", "100", "9000000000007"],
        ],
        "text_columns": [0, 1, 2],
        "freeze_header": True,
        "auto_filter": True,
        "column_widths": [52, 16, 20],
    }
    frozen_source = json.dumps(synthetic, ensure_ascii=False, sort_keys=True)
    simulated_error = ModuleNotFoundError("No module named '@oai/artifact-tool'")
    assert xlsx_creation_plan(artifact_tool_import_error=None) == ("artifact-tool",)
    plan = xlsx_creation_plan(artifact_tool_import_error=simulated_error)
    assert plan == ("openpyxl", "xlsxwriter", "stdlib")

    spec = TabularXlsxSpec.from_mapping(synthetic)
    try:
        create_tabular_xlsx(ROOT / "forbidden.xlsx", spec)
    except ValueError as exc:
        assert "outside the repository" in str(exc)
    else:
        raise AssertionError("repo output boundary was not enforced")
    with tempfile.TemporaryDirectory(prefix="wb-core-user-artifact-") as directory:
        output = Path(directory) / "fallback.xlsx"
        result = create_tabular_xlsx(output, spec)
        assert result["backend"] == "stdlib"
        assert result["data_rows"] == 2 and result["columns"] == 3
        assert result["zip_integrity"] == "ok" and result["xml_integrity"] == "ok"
        assert output.is_file() and output.stat().st_size > 0
        verify_tabular_xlsx(output, spec)

        import openpyxl

        workbook = openpyxl.load_workbook(output, data_only=False, read_only=False)
        assert workbook.sheetnames == ["Номенклатура"]
        sheet = workbook["Номенклатура"]
        assert sheet.max_row == 3 and sheet.max_column == 3
        assert sheet.freeze_panes == "A2" and sheet.auto_filter.ref == "A1:C3"
        assert sheet["B2"].value == "0042" and sheet["C2"].value == "001234"
        assert sheet["B2"].data_type == "s" and sheet["C2"].data_type == "s"
        assert sheet["B2"].number_format == "@" and sheet["C2"].number_format == "@"

    assert json.dumps(synthetic, ensure_ascii=False, sort_keys=True) == frozen_source


def main() -> int:
    _assert_protocol_contract()
    _assert_canonical_source_acquisition_contract()
    _assert_artifact_tool_failure_falls_back_without_data_loss()
    print("user_artifact_xlsx_smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
