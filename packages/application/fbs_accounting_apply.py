"""Guarded activation/pause of the separately journaled FBS accounting book."""
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from packages.application.fbs_accounting_runtime import load, path, prepare, save, unpack, writer_lock
from packages.application.fbs_snapshot_cost import fingerprint
from packages.application.storage_registry import StoreRegistry


def target(request):
    root = Path(request["runtime_dir"]).resolve()
    if (root.parent / "app" / ".wb-core-runtime-sha").read_text().strip() != request["runtime_sha"]:
        raise ValueError("fbs_activation_runtime_drift")
    if StoreRegistry(root).load().manifest_sha256 != request["storage_manifest_sha256"]:
        raise ValueError("fbs_activation_storage_drift")
    return root



class FbsAccountingAdapter:
    def preview(self, request, operation_id):
        root = target(request)
        before, version = load(root)
        if request.get("action", "activate") == "pause":
            if before is None:
                raise ValueError("accounting_not_initialized")
            book = {**before, "active": False}
        else:
            if before is not None:
                raise ValueError("accounting_already_initialized")
            book = json.loads(Path(request["candidate_path"]).read_text())
            reviewed_at = datetime.fromisoformat(book["prepared_at"].replace("Z", "+00:00"))
            if not 0 <= (datetime.now(timezone.utc) - reviewed_at).total_seconds() <= 600:
                raise ValueError("fbs_activation_candidate_expired")
            # Rebuild every operand from current read-only sources, with only
            # the observation timestamp pinned for an exact deterministic diff.
            fresh, _ = prepare(root, opening=True, now=reviewed_at)
            if book != fresh:
                raise ValueError("fbs_activation_source_changed")
        digest = fingerprint(book)
        if digest != request["candidate_sha256"]:
            raise ValueError("fbs_activation_candidate_changed")
        return {"operation_id": operation_id, "target": str(root),
            "scope": {"action": request.get("action", "activate"), "effective_date": book["effective_date"],
                      "fbs": book["presentations"][max(book["presentations"])]["totals"]["fbs"]},
            "prestate_sha256": fingerprint(version), "candidate_sha256": digest,
            "recovery": {"kind": "isolated_immutable_revision_journal", "book": str(path(root)),
                         "previous_version": version, "pause_preserves_observer_and_history": True},
            "prepared": book, "expected_version": version}

    def apply(self, request, operation_id, preview):
        root = target(request)
        fresh = self.preview(request, operation_id)
        if any(fresh[k] != preview[k] for k in ("prestate_sha256", "candidate_sha256")):
            raise ValueError("fbs_activation_compare_failed")
        save(root, preview["prepared"], expected=preview["expected_version"], operation_id=operation_id)
        return {"operation_id": operation_id, "disposition": "submitted"}

    def readback(self, request, operation_id):
        root = target(request)
        if not path(root).exists():
            return {"operation_id": operation_id, "state": "not_submitted"}
        book, version = load(root)
        with closing(sqlite3.connect(path(root).as_uri() + "?mode=ro", uri=True)) as conn:
            conn.execute("PRAGMA query_only=ON")
            row = conn.execute("SELECT version,payload FROM accounting_revisions WHERE operation_id=?", (operation_id,)).fetchone()
            admitted = unpack(conn, row[1]) if row else None
        if row is None:
            return {"operation_id": operation_id, "state": "not_submitted"}
        good = row[0] == request["candidate_sha256"] == fingerprint(admitted)
        good = good and admitted["state"]["baseline"] == book["state"]["baseline"] and admitted["active"] == book["active"]
        return {"operation_id": operation_id, "state": "applied" if good else "failed",
                "version": version, "effective_date": book["effective_date"], "active": book["active"]}
