"""Private lab checkpoint for a completed regular API collection, before Ready derives it."""

from contextlib import contextmanager
from dataclasses import is_dataclass
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import patch

from packages.application import sheet_vitrina_v1_live_plan as live
from packages.application.ready_publication import (
    canonical,
    capture_build_inputs,
    check_build_inputs,
    digest,
    pin_parameters,
    readonly,
)
from packages.application.sheet_vitrina_v1_closed_backlog import ClosedBacklog
from packages.application.sheet_vitrina_v1_cycle import validate_collection
from packages.application.sheet_vitrina_v1_cycle_sources import (
    CollectedLivePlanSources,
    SheetVitrinaCycleSources,
    _Invocation,
)
from .full_cycle import expect
from .stage_checkpoint import file_hash, SOURCE_ID
from .state import atomic_json, read_json


# Provider records expose attributes. Preserve mappings inside them (including numeric
# keys), status dataclasses and tuples. The file cannot name executable Python classes.
def pack(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return ["map", [[pack(key), pack(item)] for key, item in value.items()]]
    if isinstance(value, (list, tuple)):
        return ["tuple" if isinstance(value, tuple) else "list", [pack(item) for item in value]]
    if (is_dataclass(value) or isinstance(value, SimpleNamespace)) and hasattr(value, "__dict__"):
        # Some adapters attach additional evidence (e.g. funnel diagnostics) to a
        # dataclass. Native source proofs hash the complete attribute dictionary.
        return ["status" if isinstance(value, live.LiveSourceStatus) else "record", pack(vars(value))]
    raise ValueError(f"Unsupported captured source type {type(value).__name__}")


def unpack(value):
    if not isinstance(value, list):
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        raise ValueError("Invalid checkpoint scalar")
    tag, data = value
    if tag == "map":
        result = {unpack(key): unpack(item) for key, item in data}
        expect(len(result) == len(data), "Duplicate checkpoint key")
        return result
    if tag in ("tuple", "list"):
        items = [unpack(item) for item in data]
        return tuple(items) if tag == "tuple" else items
    if tag in ("record", "status"):
        attrs = unpack(data)
        expect(
            isinstance(attrs, dict) and all(isinstance(k, str) and not k.startswith("__") for k in attrs),
            "Invalid source record",
        )
        return live.LiveSourceStatus(**attrs) if tag == "status" else SimpleNamespace(**attrs)
    raise ValueError("Unsupported checkpoint tag")


@contextmanager
def capture_collection():
    original = SheetVitrinaCycleSources.collected_source_summary
    captures = []

    def observe(adapter, handle, **kwargs):
        summary = original(adapter, handle, **kwargs)
        captures.append(adapter._owned_collection(handle))
        return summary

    with patch.object(SheetVitrinaCycleSources, "collected_source_summary", observe):
        yield captures


def save(entry, receipt, captures, path):
    (invocation,) = captures
    sources = invocation.sources
    kwargs = dict(invocation.kwargs)
    kwargs.pop("log", None)
    expect(
        not invocation.args
        and kwargs == dict(as_of_date=entry._cycle_as_of_date(), execution_mode="auto_daily"),
        "Unsupported source invocation",
    )
    expect(sources.capital_reader is not None, "Missing native capital reader")
    data = {key: getattr(sources, key) for key in ("scope", "slots", "effects", "inputs")}
    with capture_build_inputs(entry.runtime.db_path, runtime_dir=entry.runtime.runtime_dir) as pins:
        pin_parameters(entry.runtime.db_path)
        atomic_json(
            path,
            dict(
                format=1,
                source=read_json(SOURCE_ID),
                cycle=receipt["cycle_id"],
                job=receipt["job_id"],
                day=receipt["business_date"],
                kwargs=kwargs,
                data=pack(data),
                material=pins["material"],
                history=pins["history"],
                daily_digest=digest(canonical(entry.wb_finance_daily_block.build_daily_payload()["days"])),
            ),
        )
    return file_hash(path)


def read_sealed(path, expected_hash):
    expect(path.is_file() and not path.is_symlink(), "Invalid source checkpoint")
    raw = path.read_bytes()
    expect(hashlib.sha256(raw).hexdigest() == expected_hash, "Source checkpoint checksum differs")
    return json.loads(raw)


def restore(entry, source, path, expected_hash, finance, fbs):
    saved = read_sealed(path, expected_hash)
    expect(
        saved["format"] == 1
        and saved["source"] == read_json(SOURCE_ID)
        and (saved["cycle"], saved["job"], saved["day"])
        == (source["cycle_id"], source["job_id"], source["business_date"])
        and saved["kwargs"] == dict(as_of_date=entry._cycle_as_of_date(), execution_mode="auto_daily"),
        "Checkpoint code, source or request differs",
    )
    versions = source["final_versions"]
    entry._cycle_validate_predecessors(
        finance, fbs, {**versions, "daily_projection_digest": saved["daily_digest"]}
    )
    adapter = entry._cycle_sources()
    closed = ClosedBacklog(adapter)
    expect(digest(canonical(closed.status())) == versions["closed_receipt_digest"], "Closed backlog changed")
    sources = live._CollectedBuildSources(**unpack(saved["data"]))
    expect(
        sources.scope[2] == source["business_date"] and sources.scope[1] == saved["kwargs"]["as_of_date"],
        "Collection dates differ",
    )
    for identity, (status, _) in sources.slots.items():
        expect(
            isinstance(status, live.LiveSourceStatus)
            and len(identity) == 5
            and identity[:3] == (status.source_key, status.temporal_slot, status.column_date),
            "Source slot identity differs",
        )
    with readonly(entry.runtime.db_path) as conn:
        check_build_inputs(conn, dict(**sources.inputs, material=saved["material"], history=saved["history"]))
    block = adapter.block
    handle = CollectedLivePlanSources()
    adapter._source_collections[handle] = _Invocation(
        sources,
        (),
        saved["kwargs"],
        (block, entry.runtime, entry.runtime.runtime_dir.resolve(), entry.runtime.db_path.resolve()),
    )
    proof = validate_collection(adapter.collected_source_summary(handle), require_full_scope=True)
    original = next(row for row in source["stages"] if row["stage"] == "api_sources")
    expect(
        proof.versions == original["versions"] and list(proof.warnings) == original["warnings"],
        "Restored collection differs from the native API receipt",
    )
    sources.capital_reader = live.OwnProductCapitalBlock(runtime=entry.runtime)
    return adapter, handle, closed
