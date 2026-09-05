#!/usr/bin/env python3
"""Small deterministic smoke for the check selector."""

from select_checks import PlanError, build_plan_from_paths, verify_plan


BASE = "1" * 40
HEAD = "2" * 40


def exists(_head: str, path: str) -> bool:
    return path in {
        "docs/example.md",
        "packages/application/finance_value.py",
        "packages/application/finance_value_smoke.py",
        "packages/application/web_vitrina_value.py",
        "apps/example.py",
        "apps/example_smoke.py",
        "unknown.bin",
    }


def main() -> None:
    docs = build_plan_from_paths(
        pull_request=1, base=BASE, head=HEAD, paths=["docs/example.md"], file_exists=exists
    )
    verify_plan(docs)
    assert docs["release_kind"] == "repo_only"
    assert docs["commands"] == []

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

    try:
        build_plan_from_paths(
            pull_request=4, base=BASE, head=HEAD, paths=["unknown.bin"], file_exists=exists
        )
    except PlanError:
        pass
    else:
        raise AssertionError("unknown path was accepted")

    deleted_history = build_plan_from_paths(
        pull_request=5,
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
