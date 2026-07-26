#!/usr/bin/env python3
"""Inventory SQLite opens and enforce registry-only migrated runtime modules."""

from __future__ import annotations

import argparse
import ast
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
MIGRATED_RUNTIME_MODULES = frozenset(
    {
        "packages/application/wb_finance_weekly.py",
        "packages/application/partner_report.py",
        "packages/application/finance_raw_storage.py",
    }
)
_IGNORED_PARTS = frozenset({".git", "__pycache__"})


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _call_name(call: ast.Call) -> str:
    try:
        return ast.unparse(call.func)
    except (AttributeError, ValueError):
        return type(call.func).__name__


def _argument_expression(call: ast.Call) -> str:
    if not call.args:
        return ""
    try:
        return ast.unparse(call.args[0])[:500]
    except (AttributeError, ValueError):
        return type(call.args[0]).__name__


def _classify_call(call_name: str) -> str | None:
    normalized = call_name.replace(" ", "")
    if normalized.endswith("sqlite3.connect") or normalized == "sqlite3.connect":
        return "direct_sqlite"
    if normalized.endswith("connect_sqlite") or normalized == "connect_sqlite":
        return "observed_helper"
    if (
        normalized.endswith("store_registry.connect")
        or normalized.endswith("registry.connect")
        or normalized.endswith("store_registry.session")
        or normalized.endswith("registry.session")
    ):
        return "logical_store_registry"
    if normalized.endswith(".connect") and (
        "StoreRegistry" in normalized or "store_registry" in normalized
    ):
        return "logical_store_registry"
    return None


def _surface(relative_path: str) -> str:
    name = Path(relative_path).name
    if (
        name.endswith("_smoke.py")
        or name.endswith("_test.py")
        or "/fixtures/" in relative_path
    ):
        return "test"
    if relative_path.startswith("packages/application/"):
        return "runtime_application"
    if relative_path.startswith("packages/adapters/"):
        return "runtime_adapter"
    if relative_path.startswith("apps/"):
        return "repo_owned_runner"
    return "other"


def _python_files(root: Path) -> Iterable[Path]:
    for top in ("apps", "packages"):
        base = root / top
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if not any(part in _IGNORED_PARTS for part in path.parts):
                yield path


def inventory(root: Path) -> dict[str, Any]:
    opens: list[dict[str, Any]] = []
    parse_errors: list[dict[str, str]] = []
    for path in _python_files(root):
        relative = path.relative_to(root).as_posix()
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=relative)
        except (OSError, SyntaxError, UnicodeDecodeError) as exc:
            parse_errors.append({"file": relative, "error": type(exc).__name__})
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            call_name = _call_name(node)
            kind = _classify_call(call_name)
            if kind is None:
                continue
            opens.append(
                {
                    "file": relative,
                    "line": int(getattr(node, "lineno", 0)),
                    "surface": _surface(relative),
                    "kind": kind,
                    "call": call_name,
                    "path_expression": _argument_expression(node),
                    "migrated_module": relative in MIGRATED_RUNTIME_MODULES,
                }
            )
    opens.sort(key=lambda item: (item["file"], item["line"], item["kind"]))
    violations = [
        item
        for item in opens
        if item["migrated_module"] and item["kind"] != "logical_store_registry"
    ]
    counts = Counter((item["surface"], item["kind"]) for item in opens)
    payload: dict[str, Any] = {
        "contract_version": "wb_core_sqlite_open_inventory_v1",
        "root": ".",
        "migrated_runtime_modules": sorted(MIGRATED_RUNTIME_MODULES),
        "open_count": len(opens),
        "counts": [
            {"surface": surface, "kind": kind, "count": count}
            for (surface, kind), count in sorted(counts.items())
        ],
        "opens": opens,
        "parse_errors": parse_errors,
        "violations": violations,
        "status": "ok" if not parse_errors and not violations else "blocked",
    }
    payload["fingerprint"] = _digest(payload)
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--check-migrated",
        action="store_true",
        help="Exit non-zero if a migrated runtime module bypasses StoreRegistry.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = inventory(args.root.resolve())
    rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    if args.check_migrated and payload["status"] != "ok":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
