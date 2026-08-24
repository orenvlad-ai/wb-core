# GitHub Release Runner Shadow Admission

## Status And Boundary

`apps/github_release_runner_shadow_admission.py` is the first isolated shadow
component for a possible future one-shot Release Runner. It is a pure,
non-authoritative function over an already collected frozen snapshot. It does
not collect or refresh GitHub state and it does not perform any release action.

The legacy GitHub Release Train described in
[`11_github_release_train.md`](11_github_release_train.md) remains the only
active release actor. A shadow `eligible` decision does not authorize branch
sync, workflow dispatch, merge, deploy, label/comment changes, or any other
GitHub, runtime, or production action.

The component has no CLI, environment reads, filesystem writes, GitHub API or
network adapter, workflow trigger, polling, process execution, SSH, runtime
access, or data action. It does not import the Release Train, its specification,
or another legacy runtime/state-machine module.

## Frozen Input

The input is a JSON-decoded object with schema
`wb-core.release-runner.repo-only-snapshot/v1` and exactly these fields:

| Field | Contract |
| --- | --- |
| `schema` | exact `wb-core.release-runner.repo-only-snapshot/v1` |
| `repository` | canonical `orenvlad-ai/wb-core` |
| `pr_number` | positive integer |
| `state`, `draft` | `open` and `false` |
| `base_repository`, `base_ref`, `base_sha` | same repository, `main`, exact 40-hex SHA |
| `head_repository`, `head_ref`, `head_sha` | same repository, non-empty ref, exact 40-hex SHA |
| `expected_head_sha` | exact 40-hex SHA equal to `head_sha` |
| `labels` | JSON string array |
| `required_check` | exact object `{id,name,app_slug,head_sha,status,conclusion}` |
| `mergeable` | JSON `true`, `false`, or `null` |

An eligible snapshot has exactly one `task:*` label (`task:standard`), exactly
one `scope:*` label (`scope:repo-only`), includes `release:ready`, and has no
conflicting `release:*` state. Unrelated ordinary labels remain digest-bound.
`required_check` must bind a positive check-run id, `baseline`,
`github-actions`, the exact PR head, `completed`, and `success`.

The shadow explicitly rejects DCP handoff branches/signals, any `finance:*`
lease signal, live/runtime or production-mutation scope, and retained LOOP,
recovery, readmission, orchestration, or legacy labels/states. Those contours
remain owned by their current fail-closed contracts and are not candidates for
this first shadow.

## Deterministic Receipt

The output schema is
`wb-core.release-runner.repo-only-admission-receipt/v1`:

```json
{
  "schema": "wb-core.release-runner.repo-only-admission-receipt/v1",
  "decision": "eligible",
  "reason_codes": [],
  "snapshot_sha256": "<64 lowercase hex>",
  "bindings": {
    "repository": "orenvlad-ai/wb-core",
    "pr_number": 1042,
    "base": {"repository": "orenvlad-ai/wb-core", "ref": "main", "sha": "..."},
    "head": {"repository": "orenvlad-ai/wb-core", "ref": "branch", "sha": "..."},
    "expected_head_sha": "...",
    "required_check": {"id": 1, "name": "baseline", "app_slug": "github-actions", "head_sha": "...", "status": "completed", "conclusion": "success"},
    "task": ["task:standard"],
    "scope": ["scope:repo-only"]
  }
}
```

Object key order is normalized by canonical JSON encoding. Labels are trimmed,
lowercased, deduplicated, and sorted; SHA values are trimmed and lowercased.
Other schema strings are trimmed but remain case-sensitive and fail closed when
they differ from their exact canonical values.
The normalized complete snapshot is encoded as UTF-8 JSON with sorted keys and
no insignificant whitespace, then hashed into `snapshot_sha256`. The same
canonical encoder makes the complete receipt byte-stable. There are no
timestamps, random values, or prose reasons.

`decision=eligible` is possible only when `reason_codes` is empty. Otherwise
the decision is `blocked`, and every applicable reason is returned once in
this fixed order:

1. `snapshot-schema-unsupported`
2. `snapshot-shape-invalid`
3. `repository-not-canonical`
4. `pr-number-invalid`
5. `pr-not-open`
6. `pr-draft`
7. `base-repository-mismatch`
8. `base-not-main`
9. `base-sha-invalid`
10. `head-repository-mismatch`
11. `head-ref-missing`
12. `head-sha-invalid`
13. `expected-head-sha-invalid`
14. `head-not-expected`
15. `task-label-not-standard`
16. `scope-label-not-repo-only`
17. `release-ready-missing`
18. `release-state-conflict`
19. `dcp-handoff-unsupported`
20. `finance-lease-unsupported`
21. `legacy-contour-unsupported`
22. `required-check-id-invalid`
23. `required-check-name-mismatch`
24. `required-check-source-mismatch`
25. `required-check-head-mismatch`
26. `required-check-not-completed`
27. `required-check-not-successful`
28. `mergeability-unknown`
29. `mergeability-false`

## Atomicity And Activation Stop Line

This shadow receives one caller-supplied snapshot. It has no collector,
refetch, compare-and-swap, event binding, or atomicity proof across GitHub API
reads. Its digest proves only which normalized input bytes were evaluated; it
does not prove that the snapshot is current or coherently collected.
Because this v1 input has no repository-global lease observation, an eligible
receipt also does not prove that no Finance lease exists outside the supplied
PR labels. An authoritative collector must close that gap before activation.

Activation requires a separate future PR and a separate owner decision. That
future scope must define authoritative collection/refetch and atomicity,
integration with current exact-head Release Train gates, rollout and rollback,
and must re-prove that no DCP, Finance, legacy, live/runtime, or production
mutation authority was broadened. Until then the receipt is advisory only.

## Verification

`python3 apps/github_release_runner_shadow_admission_smoke.py` covers the golden
eligible receipt; key/label order and digest stability; full ordered multiple
reasons; PR/base/fork/SHA/head drift; task/scope/release conflicts; DCP,
Finance, and legacy rejection; every required-check gate; mergeability
`null`/`false`; byte-stable JSON; and the absence of forbidden imports and
adapters. This smoke is intentionally not wired into `baseline-ci.yml` in the
first shadow PR.
