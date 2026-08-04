"""Mechanical entrypoint for cheap Global Watcher heartbeat classification.

This driver performs fresh GitHub queue and trusted-main checks without asking
the model to interpret their raw payloads.  The local registry remains the
durable state machine; this file only composes its read-only inputs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.codex_task_orchestrator import (  # noqa: E402
    DEFAULT_HOME,
    Registry,
)
from apps.github_release_train import (  # noqa: E402
    _queue_status_api_from_env,
    queue_status_snapshot,
)


def _git(*args: str) -> bytes:
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return result.stdout


def trusted_protocol_identity(*, fetch: bool = True) -> dict[str, object]:
    if fetch:
        subprocess.run(
            ["git", "fetch", "--quiet", "--no-tags", "origin", "main"],
            cwd=ROOT,
            check=True,
        )
    trusted_main_sha = _git("rev-parse", "refs/remotes/origin/main").decode(
        "ascii"
    ).strip()
    trusted_contract = json.loads(
        _git(
            "show",
            f"{trusted_main_sha}:packages/contracts/codex_watcher_v1.json",
        )
    )
    paths = list(
        trusted_contract.get("quiet_fast_path", {}).get(
            "protocol_digest_paths",
            [
                "AGENTS.md",
                "docs/architecture/12_codex_global_orchestration.md",
                "docs/policies/codex_watcher_prompt_v1.md",
                "packages/contracts/codex_watcher_v1.json",
                "apps/codex_task_orchestrator.py",
                "apps/codex_task_orchestrator_spec.py",
            ],
        )
    )
    digest = hashlib.sha256()
    for path in paths:
        normalized = str(path).strip()
        if not normalized:
            raise ValueError("protocol digest paths must be non-empty")
        content = _git("show", f"{trusted_main_sha}:{normalized}")
        digest.update(normalized.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return {
        "trusted_main_sha": trusted_main_sha,
        "protocol_digest": "sha256:" + digest.hexdigest(),
        "protocol_digest_paths": paths,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Mechanical preflight for the wb-core Global Watcher"
    )
    parser.add_argument("--home", default="")
    parser.add_argument("--generation", type=int, required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument(
        "--no-fetch",
        action="store_true",
        help="use the existing origin/main ref; intended only for deterministic smoke",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    home = Path(
        args.home
        or os.environ.get("WB_CORE_ORCHESTRATOR_HOME")
        or DEFAULT_HOME
    )
    registry = Registry(home)
    registry.initialize()
    try:
        protocol = trusted_protocol_identity(fetch=not args.no_fetch)
        queue_snapshot = queue_status_snapshot(
            _queue_status_api_from_env(),
            release_proof_prs=registry.pending_release_lane_owner_prs(),
        )
        result = registry.classify_watcher_run(
            generation=args.generation,
            owner=args.owner,
            queue_snapshot=queue_snapshot,
            trusted_main_sha=str(protocol["trusted_main_sha"]),
            protocol_digest=str(protocol["protocol_digest"]),
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:
        error_identity = f"{type(exc).__module__}.{type(exc).__qualname__}"
        print(
            json.dumps(
                {
                    "status": "error",
                    "fallback": "FULL",
                    "reason": "mechanical-preflight-error",
                    "error_digest": "sha256:"
                    + hashlib.sha256(error_identity.encode("utf-8")).hexdigest(),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
