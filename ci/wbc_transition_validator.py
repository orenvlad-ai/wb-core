#!/usr/bin/env python3
"""One-time base-owned validator for replacing the PR/release boundary."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path


SCHEMA = "wb-core.transition-validator/v1"
SHA_RE = re.compile(r"[0-9a-f]{40}")
PROTECTED_PATHS = {
    ".github/workflows/pr-gate.yml",
    ".github/workflows/release-runner.yml",
    "apps/github_release_runner.py",
}
CONTROL_PATHS = {
    ".github/workflows/wbc-transition-validator.yml",
    "ci/wbc_transition_validator.py",
}


def exact_sha(value: str, label: str) -> str:
    normalized = value.strip().lower()
    if SHA_RE.fullmatch(normalized) is None:
        raise SystemExit(f"{label} is not a full commit SHA")
    return normalized


def changed_paths(root: Path, base: str, head: str) -> list[str]:
    output = subprocess.run(
        ["git", "diff", "--name-only", "--find-renames", f"{base}...{head}"],
        cwd=root,
        check=True,
        text=True,
        capture_output=True,
    ).stdout
    return sorted({line.strip() for line in output.splitlines() if line.strip()})


def require_text(path: Path, fragments: tuple[str, ...], forbidden: tuple[str, ...]) -> None:
    text = path.read_text(encoding="utf-8")
    missing = [fragment for fragment in fragments if fragment not in text]
    present = [fragment for fragment in forbidden if fragment in text]
    if missing or present:
        raise SystemExit(
            f"unsafe boundary file {path}: missing={missing!r}, forbidden={present!r}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    root = args.root.resolve()
    base = exact_sha(args.base, "base")
    head = exact_sha(args.head, "head")
    if args.pr <= 0:
        raise SystemExit("invalid pull request number")
    actual = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, text=True, capture_output=True
    ).stdout.strip()
    if actual != head:
        raise SystemExit(f"candidate mismatch: expected {head}, got {actual}")

    paths = changed_paths(root, base, head)
    protected = sorted(PROTECTED_PATHS.intersection(paths))
    if not protected:
        raise SystemExit("transition validator ran without a protected boundary change")
    changed_controls = sorted(CONTROL_PATHS.intersection(paths))
    if changed_controls:
        raise SystemExit(f"candidate changed its trusted transition controls: {changed_controls}")

    require_text(
        root / ".github/workflows/pr-gate.yml",
        (
            "name: PR Gate",
            "pull_request:",
            "contents: read",
            "name: pr-gate",
            "github.event.pull_request.base.sha",
            "trusted-base/ci/run_checks.py",
        ),
        ("pull_request_target:", "secrets."),
    )
    require_text(
        root / ".github/workflows/release-runner.yml",
        ("name: Release Runner", "workflow_run:", "ref: main", "apps/github_release_runner.py"),
        ("pull_request_target:",),
    )

    source = Path(__file__).read_bytes()
    receipt = {
        "schema": SCHEMA,
        "pull_request": args.pr,
        "base_sha": base,
        "head_sha": head,
        "protected_paths": protected,
        "validator_sha256": hashlib.sha256(source).hexdigest(),
        "result": "success",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
