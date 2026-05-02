"""Local repo-only runner CLI for curator cockpit MVP execution artifacts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Callable, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.curator_cockpit_mvp_execution import (  # noqa: E402
    CuratorCockpitExecutionError,
    cleanup_run_worktree,
    prepare_run,
    run_result_to_dict,
    run_step,
    verifier_result_to_dict,
    verify_run,
)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.handler(args)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Repo-only curator cockpit MVP runner.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare_parser = subparsers.add_parser("prepare-run")
    _add_run_inputs(prepare_parser)
    prepare_parser.set_defaults(handler=_handle_prepare_run)

    run_parser = subparsers.add_parser("run-step")
    _add_run_inputs(run_parser)
    run_parser.add_argument("--executor-mode", choices=("fake", "command"), default="fake")
    run_parser.add_argument("--allow-real-executor", action="store_true")
    run_parser.add_argument("--executor-command")
    run_parser.add_argument("--cleanup", action="store_true")
    run_parser.set_defaults(handler=_handle_run_step)

    verify_parser = subparsers.add_parser("verify-run")
    verify_parser.add_argument("--run-dir", required=True, type=Path)
    verify_parser.set_defaults(handler=_handle_verify_run)

    cleanup_parser = subparsers.add_parser("cleanup-run")
    cleanup_parser.add_argument("--run-dir", required=True, type=Path)
    cleanup_parser.set_defaults(handler=_handle_cleanup_run)

    return parser


def _add_run_inputs(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--task-spec", required=True, type=Path)
    parser.add_argument("--step-id", required=True)
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--base-ref")
    parser.add_argument("--branch-name")


def _handle_prepare_run(args: argparse.Namespace) -> int:
    return _run_json_command(lambda: _prepare_run_summary(args))


def _handle_run_step(args: argparse.Namespace) -> int:
    return _run_json_command(lambda: _run_step_summary(args))


def _handle_verify_run(args: argparse.Namespace) -> int:
    return _run_json_command(lambda: _verify_run_summary(args.run_dir))


def _handle_cleanup_run(args: argparse.Namespace) -> int:
    return _run_json_command(lambda: (cleanup_run_worktree(args.run_dir), 0))


def _prepare_run_summary(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    payload = _read_json(args.task_spec)
    result = prepare_run(
        payload,
        step_id=args.step_id,
        repo_root=args.repo_root,
        state_dir=args.state_dir,
        base_ref=args.base_ref,
        branch_name=args.branch_name,
    )
    summary = _summary_from_run_result(result)
    summary["verifier_status"] = None
    return summary, 0


def _run_step_summary(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    payload = _read_json(args.task_spec)
    result = run_step(
        payload,
        step_id=args.step_id,
        repo_root=args.repo_root,
        state_dir=args.state_dir,
        base_ref=args.base_ref,
        branch_name=args.branch_name,
        executor_mode=args.executor_mode,
        allow_real_executor=args.allow_real_executor,
        executor_command=args.executor_command,
    )
    summary = _summary_from_run_result(result)
    summary["verifier_status"] = "passed" if result.status == "verifier_passed" else result.status
    if args.cleanup:
        summary["cleanup"] = cleanup_run_worktree(Path(result.run_dir))
    return summary, 0 if result.status == "verifier_passed" else 1


def _verify_run_summary(run_dir: Path) -> tuple[dict[str, Any], int]:
    verifier = verify_run(run_dir)
    summary = {
        "status": "verified" if verifier.status == "passed" else "verification_failed",
        "run_dir": str(run_dir),
        "verifier_status": verifier.status,
        "changed_files": list(verifier.changed_files),
        "forbidden_path_hits": list(verifier.forbidden_path_hits),
        "mandatory_handoff_blocks_present": verifier.mandatory_handoff_blocks_present,
        "check_results": [check for check in verifier_result_to_dict(verifier)["check_results"]],
        "blocker_reason": verifier.blocker_reason,
    }
    return summary, 0 if verifier.status == "passed" else 1


def _summary_from_run_result(result) -> dict[str, Any]:
    payload = run_result_to_dict(result)
    return {
        "status": result.status,
        "run_id": result.id,
        "task_spec_id": result.task_spec_id,
        "step_id": result.step_id,
        "branch_name": result.branch_name,
        "run_dir": result.run_dir,
        "worktree_path": result.worktree_path,
        "prompt_path": result.prompt_path,
        "handoff_path": result.handoff_path,
        "log_path": result.log_path,
        "changed_files": payload["changed_files"],
        "check_results": payload["check_results"],
        "blocker_reason": result.blocker_reason,
        "next_manual_step": result.next_manual_step,
    }


def _run_json_command(callback: Callable[[], tuple[dict[str, Any], int]]) -> int:
    try:
        summary, exit_code = callback()
    except Exception as exc:
        summary = {
            "status": "error",
            "run_id": None,
            "validation_ok": False,
            "errors": [str(exc)],
            "blocker_reason": str(exc),
        }
        exit_code = 1
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return exit_code


def _read_json(path: Path) -> Mapping[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise CuratorCockpitExecutionError("JSON root must be an object")
    return payload


if __name__ == "__main__":
    raise SystemExit(main())
