"""Owned synthetic repair -> rolling provenance continuity, no native inputs."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps.web_vitrina_history_group_repair_smoke import fixture, object_rows, GROUP, SKU
from apps.web_vitrina_history_store_smoke import DAYS
from packages.application.web_vitrina_history_compiler import digest
from packages.application.web_vitrina_history_store import HistoryStore, _read
from packages.application.web_vitrina_history_group_repair import (
    build_group_repair_candidate, preview_group_repair, publish_group_repair,
)


def load_unit(store, day):
    blobs, metadata = object_rows(store, store.edition()["days"][day])
    unit = json.loads(metadata)
    unit["cells"] = {row: json.loads(zlib.decompress(blob)) for row, blob in blobs.items()}
    return unit


def main():
    with tempfile.TemporaryDirectory(prefix="group-repair-rolling-") as temporary:
        root = Path(temporary)
        store, base, old = fixture(root)
        candidate = HistoryStore(root / "repair", max_store_bytes=64 * 1024**2)
        catalog = _read(store.root / "catalogs" / (old["catalog"] + ".json"))
        metadata = {"transform_code_hash": digest("owned-synthetic-transform"),
            "auxiliary_digests": {day: {"fixture": digest(day)} for day in DAYS}}

        def transform(day, dated_catalog, cells):
            cell = deepcopy(cells[GROUP[0]])
            if day != DAYS[2]:
                value = sum(cells[row][0] for row in SKU)
                cell[:2] = [value, str(value)]
            # The final allowlisted day deliberately has no changed object.
            return {"patch": {GROUP[0]: cell},
                    "auxiliary_digests": metadata["auxiliary_digests"][day]}

        built = build_group_repair_candidate(store, candidate, expected_base=base,
            allowlisted_group_row_ids={day: [GROUP[0]] for day in DAYS},
            metadata=metadata, transform_day=transform, max_days_per_call=3)
        assert built["status"] == "ready"
        args = {"expected_base": base, "expected_candidate": built["edition_id"]}
        preview = preview_group_repair(store, candidate, **args)
        assert preview["changed_group_cells"] == 2
        published = publish_group_repair(store, candidate, **args,
            preview_token=preview["preview_token"], revalidate=lambda: preview["repair_claim"])
        assert published["status"] == "published"
        repaired = store.edition()
        claim = deepcopy(repaired["group_repair"])
        repaired_refs = {day: repaired["days"][day] for day in DAYS[:2]}
        assert repaired["days"][DAYS[2]] == claim["base_objects"][DAYS[2]]
        assert "group_repair_retained_days" not in repaired
        archived_bytes = object_rows(store, repaired_refs[DAYS[0]])

        vector = {"coverage": "complete_frozen_native_v1", "epoch": "rolling-native-fixture",
                  "dates": {day: digest([day, "first-native-proof"]) for day in DAYS}}
        calls = []

        def refresh(cycle, *, backfill=()):
            def compile_day(day):
                calls.append(day)
                unit = load_unit(store, day)
                unit["context_epoch"] = catalog["context_epoch"]
                cell = unit["cells"][GROUP[0]]
                # Explicit synthetic compiler output ensures a new object ref.
                cell[:2] = [cycle, str(cycle)]
                return unit
            result = store.update(vector=vector, catalog=catalog, compile_day=compile_day,
                revalidate=lambda: vector, business_date="2026-05-02",
                backfill_dates=list(backfill), expected_base=digest(store.edition()))
            assert result["status"] == "published", result
            return store.edition()

        first = refresh(101)
        assert calls == DAYS[1:]
        assert first["group_repair"] == claim
        assert first["group_repair_retained_days"] == {DAYS[0]: repaired_refs[DAYS[0]]}
        assert first["days"][DAYS[1]] != repaired_refs[DAYS[1]]
        assert object_rows(store, first["days"][DAYS[0]]) == archived_bytes
        assert first["day_proofs"][DAYS[0]] == repaired["day_proofs"][DAYS[0]]
        assert first["day_catalogs"][DAYS[0]] == repaired["day_catalogs"][DAYS[0]]

        vector["dates"][DAYS[2]] = digest("second-native-proof")
        calls.clear()
        second = refresh(102)
        assert calls == DAYS[2:]
        assert second["group_repair"] == claim
        assert second["group_repair_retained_days"] == first["group_repair_retained_days"]
        assert all(second["days"][day] == ref for day, ref in second["group_repair_retained_days"].items())

        # Status-only publication must also keep the current exact claim/map.
        status = store.update(vector=vector, catalog=catalog,
            compile_day=lambda day: (_ for _ in ()).throw(AssertionError("no dirty day")),
            revalidate=lambda: vector, business_date="2026-05-02")
        assert status["status"] == "unchanged"
        assert store.edition()["group_repair_retained_days"] == second["group_repair_retained_days"]

        # An explicit native backfill replaces the final repaired archive ref.
        vector["dates"][DAYS[0]] = digest("archive-native-backfill")
        calls.clear()
        final = refresh(103, backfill=[DAYS[0]])
        assert calls == DAYS[:1]
        assert final["days"][DAYS[0]] != repaired_refs[DAYS[0]]
        assert "group_repair" not in final and "group_repair_retained_days" not in final
        print(json.dumps({"status": "pass", "fixture_only": True,
            "repair_then_recent_rolling_retains_archive_claim": True,
            "retained_days_match_exact_object_refs": True,
            "unchanged_allowlisted_day_excluded": True,
            "further_cycle_and_status_preserve_claim": True,
            "all_repaired_objects_replaced_drops_claim": True}))


if __name__ == "__main__":
    main()
