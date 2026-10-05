"""Local synthetic guard/storage checks; not native transform or production proof."""
from contextlib import closing
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from unittest.mock import patch
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps.web_vitrina_history_store_smoke import DAYS, expect_error, setup_units, vector_for
from packages.application.web_vitrina_history_compiler import digest
from packages.application.web_vitrina_history_store import HistoryStore, HistoryUnavailable, _atomic, _json, _read
from packages.application.web_vitrina_history_group_repair import (
    PENDING, READY, build_group_repair_candidate, preview_group_repair, publish_group_repair,
)

TOTAL = "TOTAL|stock"
SKU = ["SKU:100|stock", "SKU:101|stock"]
GROUP = ["GROUP:clean|stock", "GROUP:clean|mean", "GROUP:clean|untouched"]


def fixture(root):
    catalog, units = setup_units()
    for row_id in SKU + GROUP:
        row = deepcopy(catalog["rows"][TOTAL])
        row.update(row_id=row_id, row_kind="sku" if row_id in SKU else "group", group_id="group:clean")
        catalog["rows"][row_id] = row
        catalog["order"].append(row_id)
    for index, unit in enumerate(units.values()):
        for row_id in SKU + GROUP:
            cell = deepcopy(unit["cells"][TOTAL])
            value = (10 if row_id == SKU[0] else 20) + index if row_id in SKU else 999
            cell[0:2] = [value, str(value)]
            unit["cells"][row_id] = cell
            unit["members"].append(row_id)
    source = HistoryStore(root / "source", max_store_bytes=64 * 1024**2)
    vector = vector_for(units)
    initial = source.update(vector=vector, catalog=catalog,
                            compile_day=lambda day: deepcopy(units[day]), revalidate=lambda: vector)
    assert initial["status"] == "published"
    # A retained archived day has its own catalog/context/native proof.
    dated_catalog = deepcopy(catalog)
    dated_catalog["context_epoch"] = "archived-catalog-epoch"
    dated_unit = deepcopy(units[DAYS[0]])
    dated_unit["context_epoch"] = dated_catalog["context_epoch"]
    with source._writer():
        catalog_id = digest(dated_catalog)
        _atomic(source.root / "catalogs" / (catalog_id + ".json"), dated_catalog)
        old = source.edition()
        old["days"][DAYS[0]] = source._write_day(dated_unit)
        old["day_catalogs"] = {d: catalog_id if d == DAYS[0] else old["catalog"] for d in DAYS}
        old["day_proofs"] = {d: {"epoch": "archived-proof" if d == DAYS[0] else vector["epoch"],
                               "token": digest(dated_unit) if d == DAYS[0] else vector["dates"][d]} for d in DAYS}
        old["consumed"]["dates"].pop(DAYS[0])
        old["rolling14"] = {"archive_not_reevaluated": True, "backfill_required": [DAYS[0]]}
        base = digest(old)
        _atomic(source.root / "editions" / (base + ".json"), old)
        _atomic(source.root / "CURRENT.json", {"current": base, "previous": initial["edition_id"]})
    # Different compression is still a valid stored 16-field cell. Repair must
    # copy its exact BLOB rather than recreate every cell with compression 6.
    for object_id in old["days"].values():
        with closing(sqlite3.connect(source.root / "objects" / (object_id + ".sqlite3"))) as conn, conn:
            for row_id in [TOTAL, *SKU, GROUP[2]]:
                blob = conn.execute("SELECT payload FROM cells WHERE row_id=?", (row_id,)).fetchone()[0]
                conn.execute("UPDATE cells SET payload=? WHERE row_id=?", (zlib.compress(zlib.decompress(blob), 9), row_id))
    return source, base, deepcopy(old)


def object_rows(store, object_id):
    with closing(store._open_day(store.root / "objects" / (object_id + ".sqlite3"))) as conn:
        return (dict(conn.execute("SELECT row_id,payload FROM cells")),
                conn.execute("SELECT payload FROM metadata").fetchone()[0])


def main():
    with tempfile.TemporaryDirectory(prefix="history-group-repair-") as directory:
        root = Path(directory)
        source, base, old = fixture(root)
        before_pointer = (source.root / "CURRENT.json").read_bytes()
        before_catalogs = {p.name: p.read_bytes() for p in (source.root / "catalogs").glob("*.json")}
        before_objects = {d: object_rows(source, ref) for d, ref in old["days"].items()}
        allowed = {d: GROUP[:2] for d in DAYS[:2]}
        metadata = {"transform_code_hash": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                    "auxiliary_digests": {d: {"accepted_cost_basis": digest([d, "frozen-fixture"])} for d in allowed}}
        calls = []

        def transform(day, catalog, cells):
            calls.append(day)
            assert catalog["context_epoch"] == ("archived-catalog-epoch" if day == DAYS[0] else "catalog-epoch")
            values = [cells[row][0] for row in SKU]
            result = {row: deepcopy(cells[row]) for row in GROUP[:2]}
            result[GROUP[0]][:2] = [sum(values), str(sum(values))]
            result[GROUP[1]][:2] = [sum(values) / len(values), str(sum(values) / len(values))]
            # Callback mutation cannot reach the stored source or builder baseline.
            cells[TOTAL][0] = -99999
            catalog["context_epoch"] = "must-not-escape"
            return {"patch": result, "auxiliary_digests": metadata["auxiliary_digests"][day]}

        def build(candidate, **extra):
            args = dict(expected_base=base, allowlisted_group_row_ids=allowed,
                        metadata=metadata, transform_day=transform, max_days_per_call=1)
            args.update(extra)
            return build_group_repair_candidate(source, candidate, **args)

        candidate = HistoryStore(root / "candidate", max_store_bytes=64 * 1024**2)
        first = build(candidate)
        assert first["status"] == "pending" and first["completed"] == 1
        assert calls == DAYS[:1] and not (candidate.root / READY).exists()
        assert not (candidate.root / "CURRENT.json").exists()
        expect_error(lambda: build(candidate, metadata={**metadata, "transform_code_hash": digest("other-code")}),
                     HistoryUnavailable, "resume_conflict")
        second = build(candidate)
        assert second["status"] == "ready" and second["completed"] == 2 and calls == DAYS[:2]
        assert not (candidate.root / "CURRENT.json").exists()
        assert (source.root / "CURRENT.json").read_bytes() == before_pointer
        edition_id = second["edition_id"]
        edition = _read(candidate.root / "editions" / (edition_id + ".json"))
        for key in ("catalog", "day_catalogs", "consumed", "day_proofs", "rolling14"):
            assert edition[key] == old[key], key
        assert edition["days"][DAYS[2]] == old["days"][DAYS[2]]
        assert edition["group_repair"]["base_objects"] == {d: old["days"][d] for d in allowed}
        for day in allowed:
            blobs, raw_metadata = object_rows(candidate, edition["days"][day])
            assert raw_metadata == before_objects[day][1]
            for row in [TOTAL, *SKU, GROUP[2]]:
                assert blobs[row] == before_objects[day][0][row], (day, row)
        args = dict(expected_base=base, expected_candidate=edition_id)
        preview = preview_group_repair(source, candidate, **args)
        assert preview["changed_group_cells"] == 4 and preview["days"] == 2
        assert before_catalogs == {p.name: p.read_bytes() for p in (source.root / "catalogs").glob("*.json")}

        # Three days use two immutable catalog hashes; parse each hash only once
        # per complete build/preview invocation, including repeated current days.
        all_allowed={d:GROUP[:2] for d in DAYS}
        all_metadata={"transform_code_hash":metadata["transform_code_hash"],
                      "auxiliary_digests":{d:{"receipt":digest(d)} for d in DAYS}}
        def cached_transform(day,catalog,cells):
            result={r:deepcopy(cells[r]) for r in GROUP[:2]}
            result[GROUP[0]][0]=sum(cells[r][0] for r in SKU)
            return {"patch":result,"auxiliary_digests":all_metadata["auxiliary_digests"][day]}
        import packages.application.web_vitrina_history_group_repair as repair
        original_read=repair._read;reads=Counter()
        def counted_read(path):
            if path.parent==source.root/"catalogs":reads[path.name]+=1
            return original_read(path)
        cached_candidate=HistoryStore(root/"cached-candidate")
        with patch.object(repair,"_read",side_effect=counted_read):
            cached_result=build(cached_candidate,allowlisted_group_row_ids=all_allowed,
                                metadata=all_metadata,transform_day=cached_transform,max_days_per_call=3)
        assert cached_result["status"]=="ready" and len(reads)==2 and all(n==1 for n in reads.values())
        reads.clear()
        with patch.object(repair,"_read",side_effect=counted_read):
            preview_group_repair(source,cached_candidate,expected_base=base,expected_candidate=cached_result["edition_id"])
        assert len(reads)==2 and all(n==1 for n in reads.values())

        # Guard failures preserve the same source CURRENT and immutable objects.
        expect_error(lambda: build(source), HistoryUnavailable, "overlapping_roots")
        expect_error(lambda: build(HistoryStore(root / "bad-kind"), allowlisted_group_row_ids={d: [SKU[0]] for d in allowed}),
                     HistoryUnavailable, "non_group_allowlist")
        expect_error(lambda: build(HistoryStore(root / "missing-proof"), metadata={**metadata,"auxiliary_digests":{DAYS[0]:metadata["auxiliary_digests"][DAYS[0]]}}),
                     HistoryUnavailable,"proof_invalid")
        def short_cell(day,catalog,cells):
            result=transform(day,catalog,cells);result["patch"][GROUP[0]].pop();return result
        expect_error(lambda:build(HistoryStore(root / "short-cell"),transform_day=short_cell),HistoryUnavailable,"cell_limit")
        def bad_patch(day, catalog, cells):
            result = transform(day, catalog, cells)
            result["patch"][SKU[0]] = cells[SKU[0]]
            return result
        expect_error(lambda: build(HistoryStore(root / "bad-patch"), transform_day=bad_patch),
                     HistoryUnavailable, "patch_or_proof_invalid")
        def bad_proof(day, catalog, cells):
            result = transform(day, catalog, cells)
            result["auxiliary_digests"] = {"accepted_cost_basis": digest("not-pinned")}
            return result
        expect_error(lambda: build(HistoryStore(root / "bad-proof"), transform_day=bad_proof),
                     HistoryUnavailable, "patch_or_proof_invalid")
        clock=[0.0]
        def late(day, catalog, cells):
            result = transform(day, catalog, cells)
            clock[0]=10.0
            return result
        late_candidate = HistoryStore(root / "late")
        with patch("packages.application.web_vitrina_history_group_repair.time.monotonic",side_effect=lambda:clock[0]):
            result = build(late_candidate, transform_day=late, deadline_monotonic=5.0)
        assert result["status"] == "pending" and not (late_candidate.root / READY).exists()
        assert not _read(late_candidate.root / PENDING)["refs"]
        expect_error(lambda: build(HistoryStore(root / "tiny", max_store_bytes=128)),
                     HistoryUnavailable, "storage_limit")
        with candidate._writer():
            expect_error(lambda: build(candidate), HistoryUnavailable, "builder_busy")
        assert (source.root / "CURRENT.json").read_bytes() == before_pointer

        # A rehashed, schema-valid candidate that changes a SKU is still refused.
        ready = _read(candidate.root / READY)
        broken = deepcopy(edition)
        with candidate._writer():
            day = DAYS[0]
            blobs, raw_metadata = object_rows(candidate, edition["days"][day])
            unit = json.loads(raw_metadata)
            unit["cells"] = {rid: json.loads(zlib.decompress(blob)) for rid, blob in blobs.items()}
            unit["cells"][SKU[0]][0] += 1
            broken["days"][day] = candidate._write_day(unit)
            broken_id = digest(broken)
            _atomic(candidate.root / "editions" / (broken_id + ".json"), broken)
            _atomic(candidate.root / READY, {**ready, "edition_id": broken_id})
        expect_error(lambda: preview_group_repair(source, candidate, expected_base=base, expected_candidate=broken_id),
                     HistoryUnavailable, "non_group_cell_changed")
        _atomic(candidate.root / READY, ready)

        expect_error(lambda: publish_group_repair(source, candidate, **args, preview_token=digest("wrong"),
                                                  revalidate=lambda: preview["repair_claim"]),
                     HistoryUnavailable, "preview_mismatch")
        stale = publish_group_repair(source, candidate, **args, preview_token=preview["preview_token"],
                                     revalidate=lambda: {**preview["repair_claim"], "transform_code_hash": digest("changed")})
        assert stale["status"] == "superseded" and (source.root / "CURRENT.json").read_bytes() == before_pointer
        # CAS also binds the complete CURRENT pointer, not only its current ID.
        _atomic(source.root / "CURRENT.json", {"current": base, "previous": digest("changed-pointer")})
        expect_error(lambda: preview_group_repair(source, candidate, **args), HistoryUnavailable, "claim_mismatch")
        (source.root / "CURRENT.json").write_bytes(before_pointer)
        clock=[0.0];validations=[]
        def late_revalidation():
            validations.append(1)
            if len(validations)==2:clock[0]=10.0
            return preview["repair_claim"]
        with patch("packages.application.web_vitrina_history_group_repair.time.monotonic",side_effect=lambda:clock[0]):
            deferred=publish_group_repair(source,candidate,**args,preview_token=preview["preview_token"],
                                          revalidate=late_revalidation,deadline_monotonic=5.0)
        assert deferred["status"]=="pending" and (source.root / "CURRENT.json").read_bytes()==before_pointer
        # Storage reservation itself can exhaust the remaining deadline.
        clock[0]=0.0;reserve=source._reserve
        def late_reserve(additional):
            reserve(additional)
            if additional==4096:clock[0]=10.0
        with patch("packages.application.web_vitrina_history_group_repair.time.monotonic",side_effect=lambda:clock[0]), patch.object(source,"_reserve",side_effect=late_reserve):
            deferred=publish_group_repair(source,candidate,**args,preview_token=preview["preview_token"],
                                          revalidate=lambda:preview["repair_claim"],deadline_monotonic=5.0)
        assert deferred["status"]=="pending" and (source.root / "CURRENT.json").read_bytes()==before_pointer
        writes = []
        def counted(path, value):
            if path.name == "CURRENT.json":
                writes.append(value)
            _atomic(path, value)
        with patch("packages.application.web_vitrina_history_group_repair._atomic", side_effect=counted):
            published = publish_group_repair(source, candidate, **args, preview_token=preview["preview_token"],
                                             revalidate=lambda: preview["repair_claim"])
            assert published["status"] == "published" and len(writes) == 1
            again = publish_group_repair(source, candidate, **args, preview_token=preview["preview_token"],
                                        revalidate=lambda: (_ for _ in ()).throw(AssertionError("idempotent readback must not resubmit")))
            assert again["status"] == "already_published" and len(writes) == 1
        assert source.edition()["consumed"] == old["consumed"]
        for day in allowed:
            blobs, raw_metadata = object_rows(source, source.edition()["days"][day])
            assert raw_metadata == before_objects[day][1]
            for row in [TOTAL, *SKU, GROUP[2]]:
                assert blobs[row] == before_objects[day][0][row]
        assert before_catalogs == {p.name: p.read_bytes() for p in (source.root / "catalogs").glob("*.json")}
        print('{"status":"pass","fixture_only":true,"group_only_blob_patch":true,"mixed_archived_catalog_proofs_preserved":true,"bounded_resume":true,"aux_and_code_pin":true,"deadline_storage_locks":true,"preview_and_cas":true,"single_submit_idempotent_readback":true}')


if __name__ == "__main__":
    main()
