"""Dormant owned collect/derive seam; canonical evaluator remains hash-pinned.

The short fresh-pin orchestration mirrors the original block.build_plan. No
formula, source execution or material calculation is implemented here.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
import re
from weakref import WeakKeyDictionary
from packages.application.sheet_vitrina_v1_live_plan import (
    SheetVitrinaV1LivePlanBlock, SheetVitrinaV1Envelope, _CollectedBuildSources,
    _is_valid_temporal_candidate, _plain_jsonable, ARCHIVED_ONLY_SOURCE_KEYS,
)


class CollectedLivePlanSources:
    """Opaque invocation lifetime, never a persistence or raw-outcome API."""
    __slots__ = ('__weakref__',)


@dataclass
class _Invocation:
    sources: _CollectedBuildSources
    args: tuple
    kwargs: dict
    runtime_context: tuple


class SheetVitrinaCycleSources:
    def __init__(self, block: SheetVitrinaV1LivePlanBlock):
        self.block = block
        self._source_collections: WeakKeyDictionary = WeakKeyDictionary()

    def collect_sources(self, *args, **kwargs) -> CollectedLivePlanSources:
        """Capture the existing source effects once; do not derive/publish a plan.

        Uses the same invocation arguments as build_plan, including one-shot
        selectors. Collection is not read-only: existing capture/cache effects,
        mature buyout, rollover and web sync retain their original behavior.
        """
        from packages.application.ready_publication import capture_build_inputs
        args = list(args)
        for index in (3, 4):
            if len(args) > index and args[index] is not None:
                args[index] = tuple(args[index])
        for key in ("source_keys", "metric_keys"):
            if kwargs.get(key) is not None:
                kwargs[key] = tuple(kwargs[key])
        collected = _CollectedBuildSources()
        invocation = _Invocation(collected, tuple(args), dict(kwargs),
            (self.block, self.block.runtime, Path(self.block.runtime.runtime_dir).resolve(),
             Path(self.block.runtime.db_path).resolve()))
        with capture_build_inputs(self.block.runtime.db_path, runtime_dir=self.block.runtime.runtime_dir) as source_inputs:
            self.block._build_plan(*args, **kwargs, _collection=collected, _collect_only=True)
        # Material pins from before external collection are deliberately not
        # reused. Every local operand is read again below with fresh pins.
        collected.inputs = deepcopy({key: source_inputs[key] for key in (
            "sources", "consumed", "conflicts", "authority")})
        handle = CollectedLivePlanSources()
        self._source_collections[handle] = invocation
        return handle

    def derive_collected(self, handle: CollectedLivePlanSources) -> SheetVitrinaV1Envelope:
        """Derive from an owned capture with fresh local pins and no source fetch.

        A handle binds its original request and runtime. Reuse retains source
        outcomes but reopens material/ready operands; all existing source,
        authority, scope and publication guards still apply.
        """
        from packages.application.ready_publication import (
            ReadyPublicationConflict, capture_build_inputs, capture_expected,
            check_build_inputs, check_expected, readonly,
        )
        invocation = self._owned_collection(handle)
        collected = invocation.sources
        args, kwargs = invocation.args, invocation.kwargs
        for attempt in range(1, 4):
            with capture_build_inputs(self.block.runtime.db_path, runtime_dir=self.block.runtime.runtime_dir) as inputs:
                if inputs["authority"] != collected.inputs["authority"]:
                    raise ReadyPublicationConflict("ready_collection_authority_changed")
                inputs.update(deepcopy({key: collected.inputs[key] for key in (
                    "sources", "consumed", "conflicts")}))
                try:
                    # Fail before doing local work if a consumed source already
                    # changed. Reusing older source values is not a rebase.
                    with readonly(self.block.runtime.db_path) as conn:
                        check_build_inputs(conn, inputs)
                        expected = capture_expected(conn, bundle_version=collected.scope[0],
                            as_of_date=collected.scope[1], authority=inputs["authority"])
                    plan = self.block._build_plan(*args, **kwargs, _collection=collected)
                    with readonly(self.block.runtime.db_path) as conn:
                        check_build_inputs(conn, inputs)
                        check_expected(conn, expected)
                except ReadyPublicationConflict as exc:
                    if attempt == 3 or not str(exc).startswith((
                        "ready_material_input_changed:", "ready_history_changed_during_build", "ready_target_changed:",
                    )):
                        raise
                    continue
                return replace(plan, metadata={**dict(plan.metadata or {}),
                    "publication_inputs": inputs, "local_derive_attempt": attempt,
                    "local_derive_expected_ready_fingerprint": expected.fingerprint})
        raise AssertionError("bounded local derive did not terminate")

    def _owned_collection(self, handle):
        from packages.application.ready_publication import ReadyPublicationConflict
        if not isinstance(handle, CollectedLivePlanSources) or handle not in self._source_collections:
            raise ReadyPublicationConflict("ready_collection_context_changed")
        invocation = self._source_collections[handle]
        block, runtime, runtime_dir, db_path = invocation.runtime_context
        if (block is not self.block or runtime is not self.block.runtime or runtime_dir != Path(self.block.runtime.runtime_dir).resolve()
                or db_path != Path(self.block.runtime.db_path).resolve()):
            raise ReadyPublicationConflict("ready_collection_context_changed")
        return invocation

    def collected_source_summary(self, handle: CollectedLivePlanSources) -> dict:
        """Owned copied proofs only; never expose retained payloads or attempt notes."""
        from packages.application.ready_publication import canonical, digest
        collected = self._owned_collection(handle).sources
        slots = []
        for status, payload in collected.slots.values():
            accepted = _is_valid_temporal_candidate(source_key=status.source_key,
                status=status, payload=payload, column_date=status.column_date,
                temporal_slot=status.temporal_slot)
            # Cycle acceptance never uses the injected synthetic-Finance exception.
            if status.source_key == 'fin_report_daily':
                pagination = (status.diagnostics or {}).get('pagination', {})
                accepted = accepted and pagination.get('complete') is True and pagination.get('terminal_status') == 204
            latest = (status.diagnostics or {}).get('latest_attempt', {}).get('kind')
            if not latest:
                match = re.search(r'(?:^|;)\s*latest_attempt_kind=([^;]+)', status.note)
                latest = match.group(1).strip() if match else status.kind
            policy = ('archive_only' if status.source_key in ARCHIVED_ONLY_SOURCE_KEYS
                else 'accepted_partial' if accepted and status.kind == 'incomplete'
                else 'accepted_retained' if accepted and latest != status.kind
                else 'accepted_complete' if accepted and status.kind == 'success' else 'unavailable')
            slots.append(dict(source_key=status.source_key, temporal_slot=status.temporal_slot,
                date=status.column_date, kind=status.kind, latest_attempt_kind=latest,
                accepted=bool(accepted), accepted_digest=digest(canonical(_plain_jsonable(payload))) if accepted else '',
                requested_count=status.requested_count, covered_count=status.covered_count,
                missing_count=len(status.missing_nm_ids), policy=policy))
        for item in collected.effects['collection_diagnostics']['source_slots']:
            if item.get('origin') == 'not_supported':
                slots.append(dict(source_key=item['source_key'], temporal_slot=item['slot_kind'],
                    date=item['requested_date'], kind='not_available', latest_attempt_kind='not_available',
                    accepted=False, accepted_digest='', policy='temporal_role_unavailable'))
        return dict(scope_fingerprint=digest(canonical(collected.scope)),
            provenance_fingerprint=digest(canonical(collected.inputs)),
            bundle_version=collected.scope[0], as_of_date=collected.scope[1],
            business_date=collected.scope[2], slots=slots)
