#!/usr/bin/env python3
"""Run one trusted check plan against an exact candidate checkout."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent))
from select_checks import verify_plan  # noqa: E402

SHA_RE = re.compile(r"[0-9a-f]{40}")


def local_markdown_links(root: Path, changed: list[str]) -> list[str]:
    failures: list[str] = []
    pattern = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
    for relative in changed:
        path = root / relative
        if path.suffix.lower() != ".md" or not path.is_file():
            continue
        for target in pattern.findall(path.read_text(encoding="utf-8")):
            target = target.split("#", 1)[0]
            if not target or "://" in target or target.startswith(("#", "mailto:")):
                continue
            resolved = (path.parent / target).resolve()
            if root.resolve() not in resolved.parents and resolved != root.resolve():
                failures.append(f"{relative}: link escapes repository: {target}")
            elif not resolved.exists():
                failures.append(f"{relative}: missing link target: {target}")
    return failures


def validate_formats(root: Path, changed: list[str]) -> None:
    errors = local_markdown_links(root, changed)
    for relative in changed:
        path = root / relative
        if not path.is_file():
            continue
        if path.suffix.lower() == ".json":
            try:
                json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                errors.append(f"{relative}: invalid JSON: {exc}")
    if errors:
        raise SystemExit("\n".join(errors))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    verify_plan(plan)
    expected = str(plan.get("head_sha") or "")
    actual = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, text=True, capture_output=True
    ).stdout.strip()
    if SHA_RE.fullmatch(expected) is None or actual != expected:
        raise SystemExit(f"candidate SHA mismatch: expected {expected}, got {actual}")
    changed = plan.get("changed_paths")
    commands = plan.get("commands")
    if not isinstance(changed, list) or not isinstance(commands, list):
        raise SystemExit("invalid check plan")
    validate_formats(root, [str(path) for path in changed])
    for command in commands:
        if not isinstance(command, list) or not command or command[0] not in {"python3", "node"}:
            raise SystemExit(f"unsupported command: {command!r}")
        print("check:", " ".join(command), flush=True)
        subprocess.run(command, cwd=root, check=True, timeout=900)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
