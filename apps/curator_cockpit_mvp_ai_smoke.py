"""Smoke-check for the optional curator cockpit AI intake layer."""

from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.curator_cockpit_mvp_ai import (  # noqa: E402
    CuratorDraftRequest,
    curator_draft_result_to_dict,
    draft_task_spec,
    draft_task_spec_from_model_json,
)


def main() -> None:
    request = CuratorDraftRequest(
        discussion_id="discussion-smoke",
        messages=[
            {
                "role": "operator",
                "content": "Make the local cockpit easier: draft a task spec and run the safe fake flow only.",
            }
        ],
        mode="fake",
    )
    result = draft_task_spec(request)
    if result.status != "success" or not result.task_spec:
        raise AssertionError(f"fake provider must create a valid draft task spec: {curator_draft_result_to_dict(result)}")
    task_spec = dict(result.task_spec)
    if task_spec.get("status") != "draft":
        raise AssertionError(f"fake provider must return draft status: {task_spec}")
    if not task_spec.get("task_class") or not task_spec.get("class_reason"):
        raise AssertionError(f"task_class and class_reason are required: {task_spec}")
    for path in ("wb_core_docs_master/**", "99_MANIFEST__DOCSET_VERSION.md"):
        if path not in task_spec.get("forbidden_paths", []):
            raise AssertionError(f"generated draft missing forbidden path: {path}")
    for action in ("live_deploy", "ssh", "root_shell", "public_route_change"):
        if action not in task_spec.get("forbidden_actions", []):
            raise AssertionError(f"generated draft missing forbidden action: {action}")

    invalid = draft_task_spec_from_model_json("not-json", provider="fake")
    if invalid.status != "failed" or not invalid.errors:
        raise AssertionError(f"invalid model output must fail closed: {curator_draft_result_to_dict(invalid)}")

    openai_missing_key = draft_task_spec(CuratorDraftRequest(messages=request.messages, mode="openai"), env={})
    if openai_missing_key.status != "blocked" or openai_missing_key.blocked_reason != "OPENAI_API_KEY missing":
        raise AssertionError(f"openai mode without key must block: {curator_draft_result_to_dict(openai_missing_key)}")

    secret = "test-openai-key-value"
    openai_missing_model = draft_task_spec(
        CuratorDraftRequest(messages=request.messages, mode="openai"),
        env={"OPENAI_API_KEY": secret},
    )
    serialized = json.dumps(curator_draft_result_to_dict(openai_missing_model), ensure_ascii=False)
    if openai_missing_model.status != "blocked" or openai_missing_model.blocked_reason != "CURATOR_COCKPIT_OPENAI_MODEL missing":
        raise AssertionError(f"openai mode without model must block: {serialized}")
    if secret in serialized:
        raise AssertionError("AI draft result must not expose OPENAI_API_KEY")

    print("curator-cockpit-mvp-ai-smoke passed")


if __name__ == "__main__":
    main()
