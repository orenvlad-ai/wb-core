"""One-submit adapter for explicitly reviewed, dated management estimates.

Defaults to preview. The only SQL write is CAS of existing ready plans; the
original plan images and source snapshot are saved before the transaction.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.calculation_parameters import PROXY_BLOCK_KEY, _parameters_from_row as parameters3
from packages.application.calculation_parameters_v4 import PROXY_V4_BLOCK_KEY, _parameters_from_row as parameters4
from packages.application.warehouse_sync_lock import warehouse_sync_lock
from packages.application.warehouse_functional_lock import warehouse_functional_job_lock
from packages.application.web_vitrina_management_history import digest, project, project_complete_day, data_sheet, non_target_digest, capture_current_cost_source, SOURCE


@contextmanager
def readonly(path: Path):
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    try:
        yield conn
    finally:
        conn.close()


def private_json(path: Path, value: Any) -> None:
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())


class WebVitrinaManagementHistoryAdapter:
    def target(self, request: dict[str, Any]) -> tuple[Path, Path]:
        runtime = Path(request["runtime_dir"]).resolve()
        manifest = json.loads((runtime / "storage_generation_manifest.json").read_text())
        if manifest["canonical_source"] != "split" or manifest["manifest_sha256"] != request["storage_manifest_sha256"]:
            raise ValueError("storage-authority-mismatch")
        db = (runtime / manifest["operational"]["relative_path"]).resolve()
        if not db.is_relative_to(runtime / "generations") or not db.is_file():
            raise ValueError("storage-target-invalid")
        actual = (runtime.parent / "app" / ".wb-core-runtime-sha").read_text().strip()
        if actual != request["runtime_sha"] or len(actual) != 40:
            raise ValueError("runtime-sha-mismatch")
        return runtime, db

    def build(self, request: dict[str, Any], operation_id: str, conn: Any) -> dict[str, Any]:
        source = request["source"]
        source_day = date.fromisoformat(source["column_date"])
        days = sorted(set(request["dates"]))
        if not days or len(days) > 31 or any(date.fromisoformat(d) > source_day for d in days):
            raise ValueError("estimate-date-scope-invalid")
        if digest(source) != request["source_sha256"]:
            raise ValueError("frozen-source-digest-mismatch")
        if request.get('current_only'):
            from packages.business_time import current_business_date_iso
            now=datetime.now(timezone.utc)
            if days != [current_business_date_iso(now)]: raise ValueError('current-repair-date-mismatch')
            path=Path(conn.execute('PRAGMA database_list').fetchone()[2])
            current=capture_current_cost_source(path,scopes=sorted(k.split('|')[0] for k in source['costs'] if k.startswith('SKU:')),
                bundle_version=source['bundle_version'],now=now)
            if digest(current)!=request['source_sha256']: raise ValueError('current-source-cas-drift')
        elif any(d >= source['column_date'] for d in days):
            raise ValueError('historical-repair-must-precede-source-date')
        for key, cell in source["costs"].items():
            if cell.get("source") != "official_fbs_management_inventory_v1" or cell.get("source_as_of_date") != str(source_day):
                raise ValueError("frozen-source-provenance-invalid")
            number = Decimal(cell["management_value"])
            if not number.is_finite() or number < 0:
                raise ValueError("frozen-source-cost-invalid")
        placeholders = ','.join('?' for _ in days)
        records = [dict(r) for r in conn.execute(
            "SELECT * FROM sheet_vitrina_v1_ready_snapshots snapshot WHERE bundle_version=? AND EXISTS ("
            "SELECT 1 FROM json_each(snapshot.plan_json,'$.date_columns') d WHERE d.value IN ("+placeholders+")) ORDER BY bundle_version,as_of_date",
            (source['bundle_version'], *days))]
        if len(records)>64 or sum(len(r['plan_json']) for r in records)>64*1024*1024:
            raise ValueError('bounded-snapshot-budget-exceeded')
        params, param_records = {}, []
        for day in days:
            selected = []
            for table, block, parse in (("sheet_vitrina_v1_calculation_parameter_versions", PROXY_BLOCK_KEY, parameters3),
                ("sheet_vitrina_v1_proxy_v4_parameter_versions", PROXY_V4_BLOCK_KEY, parameters4)):
                row = conn.execute("SELECT * FROM " + table + " WHERE block_key=? AND effective_date<=? ORDER BY effective_date DESC,revision DESC,created_at DESC LIMIT 1", (block, day)).fetchone()
                if row is None:
                    raise ValueError("dated-parameters-missing:" + day)
                param_records.append(dict(row))
                selected.append(parse(row))
            params[day] = tuple(selected)
        updates, changes, remaining = [], [], []
        factual_sources=request.get('complete_day_sources')
        if factual_sources:
            if '2026-09-01' not in days or conn.execute('SELECT 1 FROM sheet_vitrina_v1_ready_snapshots WHERE as_of_date=? LIMIT 1',('2026-09-01',)).fetchone():
                raise ValueError('factual-upgrade-requires-unpublished-outer-date-hole')
        for record in records:
            plan = json.loads(record["plan_json"])
            if not set(days).intersection(plan.get("date_columns", [])):
                continue
            # Explicit active bundle only; old bundles retain their own roster/history.
            if record["bundle_version"] != source["bundle_version"]:
                continue
            facts=project_complete_day(plan,sources=factual_sources,conn=conn,bundle_version=record['bundle_version'],operation_id=operation_id) if factual_sources else {'plan':plan,'changes':[]}
            result = project(facts['plan'], dates=days, source=source, parameters=params, operation_id=operation_id)
            result['changes']=facts['changes']+result['changes']
            if request.get('current_only'):
                # Current cost is an official management operand, not the
                # exceptional frozen historical cost projection.
                for change in result['changes']:
                    if change['row_id'] in source['costs']:
                        cell={**source['costs'][change['row_id']], 'operation_id':operation_id}
                        result['plan']['metadata']['server_cell_presentation'][change['row_id']][change['date']]=cell
                        change['provenance']=cell
            remaining.extend(result["remaining"])
            if result["changes"]:
                before_untouched = non_target_digest(plan, result['changes'])
                after_untouched = non_target_digest(result['plan'], result['changes'])
                if before_untouched != after_untouched:
                    raise ValueError('non-target-content-changed')
                after = json.dumps(result["plan"], ensure_ascii=False, separators=(",", ":"))
                updates.append({"bundle_version": record["bundle_version"], "as_of_date": record["as_of_date"],
                    "snapshot_id": record["snapshot_id"], "before_sha256": digest(record["plan_json"]), "after_plan_json": after,
                    "non_target_before": before_untouched, "non_target_after": after_untouched})
                changes.extend({**c, "snapshot_id": record["snapshot_id"], "outer_date": record["as_of_date"]} for c in result["changes"])
        return {"updates": updates, "changes": changes, "remaining": remaining,
            "prestate_sha256": digest({"records": records, "parameters": param_records}),
            "before_images": [r for r in records if any(u["bundle_version"] == r["bundle_version"] and u["as_of_date"] == r["as_of_date"] for u in updates)],
            "source_sha256": digest(source), "operation_id": operation_id}

    def preview(self, request: dict[str, Any], operation_id: str) -> dict[str, Any]:
        runtime, db = self.target(request)
        backup = runtime / 'evidence' / (operation_id + '.before.json')
        if backup.exists():
            candidate = json.loads(backup.read_text())['candidate']
        else:
            with readonly(db) as conn:
                candidate = self.build(request, operation_id, conn)
        return {"operation_id": operation_id, "target": str(db),
            "scope": {"dates": request["dates"], "source_snapshot_id": request["source"]["snapshot_id"],
                "changed_cells": len(candidate["changes"]), "changed_snapshots": len(candidate["updates"])},
            "prestate_sha256": candidate["prestate_sha256"], "candidate_sha256": digest(candidate),
            "recovery": {"kind": "exact-ready-plan-before-images", "path": str(runtime / "evidence" / (operation_id + ".before.json"))},
            "candidate": candidate}

    def apply(self, request: dict[str, Any], operation_id: str, preview: dict[str, Any]) -> dict[str, Any]:
        runtime, db = self.target(request)
        with warehouse_functional_job_lock(runtime, blocking=False), warehouse_sync_lock(runtime, blocking=False):
            with sqlite3.connect(db, timeout=30) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("BEGIN IMMEDIATE")
                self.target(request)
                candidate = self.build(request, operation_id, conn)
                if candidate["prestate_sha256"] != preview["prestate_sha256"] or digest(candidate) != preview["candidate_sha256"]:
                    raise ValueError("candidate-cas-drift")
                if not candidate["updates"]:
                    return {"operation_id": operation_id, "disposition": "no_change"}
                evidence = runtime / "evidence"
                evidence.mkdir(exist_ok=True)
                backup = evidence / (operation_id + ".before.json")
                before = {"operation_id": operation_id, "target": str(db), "runtime_sha": request["runtime_sha"],
                    "candidate_sha256": preview["candidate_sha256"], "candidate": candidate, "source": request["source"]}
                if backup.exists():
                    raise ValueError("operation-already-attempted-use-readback")
                private_json(backup, before)
                if digest(json.loads(backup.read_text())) != digest(before):
                    raise ValueError("backup-verification-failed")
                for update in candidate["updates"]:
                    before_row = next(r for r in candidate["before_images"] if r["bundle_version"] == update["bundle_version"] and r["as_of_date"] == update["as_of_date"])
                    count = conn.execute("UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=? WHERE bundle_version=? AND as_of_date=? AND plan_json=?",
                        (update["after_plan_json"], update["bundle_version"], update["as_of_date"], before_row["plan_json"])).rowcount
                    if count != 1:
                        raise ValueError("snapshot-cas-failed")
                conn.commit()
        return {"operation_id": operation_id, "disposition": "submitted"}

    def readback(self, request: dict[str, Any], operation_id: str) -> dict[str, Any]:
        runtime, db = self.target(request)
        backup = runtime / "evidence" / (operation_id + ".before.json")
        if not backup.exists():
            return {"operation_id": operation_id, "state": "not_submitted"}
        candidate = json.loads(backup.read_text())["candidate"]
        exact, bad = 0, []
        with readonly(db) as conn:
            for update in candidate["updates"]:
                row = conn.execute("SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots WHERE bundle_version=? AND as_of_date=?", (update["bundle_version"], update["as_of_date"])).fetchone()
                if row is None:
                    bad.append(update["as_of_date"])
                    continue
                plan = json.loads(row[0]); sheet = data_sheet(plan)
                selected_changes = [c for c in candidate['changes'] if c['snapshot_id'] == update['snapshot_id']]
                if non_target_digest(plan, selected_changes) != update['non_target_before']:
                    bad.append('non-target-drift:' + update['as_of_date'])
                rows = {r[1]: r for r in sheet["rows"]}
                for change in candidate["changes"]:
                    if change["snapshot_id"] != update["snapshot_id"]:
                        continue
                    day, key = change["date"], change["row_id"]
                    cell = plan.get("metadata", {}).get("server_cell_presentation", {}).get(key, {}).get(day, {})
                    if (day not in sheet["header"] or key not in rows or rows[key][sheet["header"].index(day)] != change["after"]
                        or cell.get("operation_id") != operation_id or cell.get("management_value") != change["provenance"]["management_value"]):
                        bad.append(key + "@" + day)
                    else:
                        exact += 1
        return {"operation_id": operation_id, "state": "applied" if not bad else "ambiguous",
                "verified_cells": exact, "mismatches": bad, "recovery_reference": str(backup)}

    def rollback(self, request: dict[str, Any], operation_id: str) -> dict[str, Any]:
        """Exact before-image recovery; refuses any post-apply snapshot drift."""
        runtime, db = self.target(request)
        candidate = json.loads((runtime / 'evidence' / (operation_id + '.before.json')).read_text())['candidate']
        with warehouse_functional_job_lock(runtime, blocking=False), warehouse_sync_lock(runtime, blocking=False):
            with sqlite3.connect(db, timeout=30) as conn:
                conn.execute('BEGIN IMMEDIATE')
                self.target(request)
                for update, before in zip(candidate['updates'], candidate['before_images']):
                    count = conn.execute('UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=? WHERE bundle_version=? AND as_of_date=? AND plan_json=?',
                        (before['plan_json'], update['bundle_version'], update['as_of_date'], update['after_plan_json'])).rowcount
                    if count != 1: raise ValueError('rollback-cas-drift')
                conn.commit()
        return {'operation_id':operation_id, 'restored_snapshots':len(candidate['updates'])}


def main() -> None:
    from apps.production_apply_launcher import execute
    envelope = json.load(sys.stdin)
    receipt = execute(action=envelope.get("action", "preview"), adapter_name=envelope.get('adapter', "web_vitrina_management_history_v1"),
        operation_id=envelope["operation_id"], request=envelope["request"],
        expected_prestate=envelope.get("expected_prestate", ""), expected_candidate=envelope.get("expected_candidate", ""))
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
