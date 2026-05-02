"""Local repo-only CLI for curator cockpit MVP contract tooling."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Callable, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.curator_cockpit_mvp import (  # noqa: E402
    CuratorCockpitValidationError,
    build_codex_prompt,
    frozen_task_spec_payload_from_mapping,
    sprint_steps_from_task_spec_mapping,
    task_spec_from_mapping,
    validate_sprint_step,
    validate_task_spec,
)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.handler(args)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Repo-only curator cockpit MVP contract tooling.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser("validate-task-spec")
    validate_parser.add_argument("--input", required=True, type=Path)
    validate_parser.set_defaults(handler=_handle_validate_task_spec)

    freeze_parser = subparsers.add_parser("freeze-task-spec")
    freeze_parser.add_argument("--input", required=True, type=Path)
    freeze_parser.add_argument("--output", required=True, type=Path)
    freeze_parser.add_argument("--frozen-at", required=False)
    freeze_parser.set_defaults(handler=_handle_freeze_task_spec)

    prompt_parser = subparsers.add_parser("generate-codex-prompt")
    prompt_parser.add_argument("--task-spec", required=True, type=Path)
    prompt_parser.add_argument("--step-id", required=True)
    prompt_parser.add_argument("--output", required=True, type=Path)
    prompt_parser.set_defaults(handler=_handle_generate_codex_prompt)

    return parser


def _handle_validate_task_spec(args: argparse.Namespace) -> int:
    return _run_json_command(lambda: _validate_task_spec_summary(args.input))


def _handle_freeze_task_spec(args: argparse.Namespace) -> int:
    return _run_json_command(lambda: _freeze_task_spec_summary(args.input, args.output, args.frozen_at))


def _handle_generate_codex_prompt(args: argparse.Namespace) -> int:
    return _run_json_command(lambda: _generate_codex_prompt_summary(args.task_spec, args.step_id, args.output))


def _validate_task_spec_summary(input_path: Path) -> tuple[dict[str, Any], int]:
    payload = _read_json(input_path)
    task_spec = task_spec_from_mapping(payload)
    validate_task_spec(task_spec)
    steps = sprint_steps_from_task_spec_mapping(payload, task_spec)
    for step in steps:
        validate_sprint_step(step)
    summary = {
        "status": "validated",
        "task_spec_id": task_spec.id,
        "task_class": task_spec.task_class,
        "validation_ok": True,
        "errors": [],
        "warnings": [],
        "allowed_actions": list(task_spec.allowed_actions),
        "forbidden_paths": list(task_spec.forbidden_paths),
        "forbidden_actions": list(task_spec.forbidden_actions),
        "steps_count": len(steps),
    }
    return summary, 0


def _freeze_task_spec_summary(input_path: Path, output_path: Path, frozen_at: str | None) -> tuple[dict[str, Any], int]:
    payload = _read_json(input_path)
    task_spec = task_spec_from_mapping(payload)
    validate_task_spec(task_spec)
    if task_spec.status != "draft":
        raise CuratorCockpitValidationError("freeze-task-spec expects a draft task spec")

    frozen_payload = frozen_task_spec_payload_from_mapping(payload, frozen_at=frozen_at)
    _write_json(output_path, frozen_payload)
    summary = {
        "status": "frozen",
        "output_path": str(output_path),
        "spec_hash": frozen_payload["spec_hash"],
        "frozen_at": frozen_payload["frozen_at"],
        "validation_ok": True,
        "errors": [],
    }
    return summary, 0


def _generate_codex_prompt_summary(task_spec_path: Path, step_id: str, output_path: Path) -> tuple[dict[str, Any], int]:
    payload = _read_json(task_spec_path)
    task_spec = task_spec_from_mapping(payload)
    validate_task_spec(task_spec, require_frozen=True)
    steps = sprint_steps_from_task_spec_mapping(payload, task_spec)
    step = _select_step(steps, step_id)
    prompt = build_codex_prompt(task_spec, step)
    _write_text(output_path, prompt)

    mandatory_blocks = {
        "classification": all(token in prompt for token in ("Класс задачи:", "Причина классификации:", "Режим выполнения:")),
        "curator_footer": "=== ДЛЯ КУРАТОРА ===" in prompt,
        "compact_check": "=== СЖАТАЯ ПРОВЕРКА ===" in prompt,
    }
    summary = {
        "status": "prompt_generated",
        "output_path": str(output_path),
        "task_class": task_spec.task_class,
        "step_id": step.id,
        "mandatory_blocks_present": all(mandatory_blocks.values()),
        "mandatory_blocks": mandatory_blocks,
    }
    return summary, 0 if summary["mandatory_blocks_present"] else 1


def _run_json_command(callback: Callable[[], tuple[dict[str, Any], int]]) -> int:
    try:
        summary, exit_code = callback()
    except Exception as exc:
        summary = {
            "status": "error",
            "task_spec_id": None,
            "task_class": None,
            "validation_ok": False,
            "errors": [str(exc)],
            "warnings": [],
        }
        exit_code = 1
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return exit_code


def _read_json(path: Path) -> Mapping[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise CuratorCockpitValidationError("JSON root must be an object")
    return payload


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _select_step(steps, step_id: str):
    for step in steps:
        if step.id == step_id:
            return step
    raise CuratorCockpitValidationError(f"sprint step not found: {step_id}")


if __name__ == "__main__":
    raise SystemExit(main())
