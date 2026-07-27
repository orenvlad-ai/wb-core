#!/usr/bin/env python3
"""Deterministic safety smoke for exact legacy storage sanitation."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.sqlite_backup_archive import verify_archive_manifest  # noqa: E402
from apps.storage_recovery_sanitation import (  # noqa: E402
    SanitationError,
    _audit_path,
    _write_audit,
    apply_family,
    inventory,
    plan_family,
)


DEPLOYED_SHA = "a" * 40


def _seed_sqlite(path: Path, value: str) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE evidence(key TEXT PRIMARY KEY,value TEXT NOT NULL)"
        )
        conn.execute("INSERT INTO evidence VALUES('scope',?)", (value,))
        conn.commit()


def _apply_current(
    *,
    runtime_dir: Path,
    root_backups: Path,
    root_name: str,
    family: str,
) -> dict:
    plan = plan_family(
        runtime_dir=runtime_dir,
        root_backups=root_backups,
        root_name=root_name,
        family=family,
        reserved_free_bytes=0,
    )
    if not plan["would_change"]:
        return plan
    return apply_family(
        runtime_dir=runtime_dir,
        root_backups=root_backups,
        root_name=root_name,
        family=family,
        fingerprint=plan["fingerprint"],
        deployed_sha=DEPLOYED_SHA,
        reserved_free_bytes=0,
    )


def run() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        base = Path(temporary)
        runtime_dir = base / "state"
        backup_root = runtime_dir / "backups"
        root_backups = base / "root" / "backups"
        runtime_dir.mkdir()
        backup_root.mkdir()
        root_backups.mkdir(parents=True)
        (runtime_dir / ".wb-core-runtime-sha").write_text(
            DEPLOYED_SHA,
            encoding="utf-8",
        )

        finance = root_backups / "wb-finance-canonical"
        finance.mkdir()
        newest = finance / "newest.sqlite3"
        older = finance / "older.sqlite3"
        _seed_sqlite(older, "older")
        _seed_sqlite(newest, "newest")
        foreign = finance / "operator-note.txt"
        foreign.write_text("keep", encoding="utf-8")

        first_plan = plan_family(
            runtime_dir=runtime_dir,
            root_backups=root_backups,
            root_name="root",
            family="wb-finance-canonical",
            reserved_free_bytes=0,
        )
        assert first_plan["action"] == "archive_raw_sqlite"
        newest.write_bytes(newest.read_bytes() + b"drift")
        try:
            apply_family(
                runtime_dir=runtime_dir,
                root_backups=root_backups,
                root_name="root",
                family="wb-finance-canonical",
                fingerprint=first_plan["fingerprint"],
                deployed_sha=DEPLOYED_SHA,
                reserved_free_bytes=0,
            )
        except SanitationError as exc:
            assert "exact current" in str(exc)
        else:
            raise AssertionError("source drift did not invalidate sanitation plan")
        # Restore a valid SQLite file after the deliberate drift check.
        newest.unlink()
        _seed_sqlite(newest, "newest")

        actions = []
        while True:
            result = _apply_current(
                runtime_dir=runtime_dir,
                root_backups=root_backups,
                root_name="root",
                family="wb-finance-canonical",
            )
            if not result.get("would_change", result.get("applied", False)):
                break
            actions.append(result)
        assert not list(finance.glob("*.sqlite3"))
        manifests = list(finance.glob("*.zst.manifest.json"))
        assert len(manifests) == 1
        verified = verify_archive_manifest(manifests[0])
        assert verified["actual_decompressed_sha256"] == verified["source_sha256"]
        assert foreign.read_text(encoding="utf-8") == "keep"

        # Stage 1 standard manifests predate source_mtime_ns. Their verified
        # archived_at remains an exact, timezone-aware generation-order fallback.
        legacy_manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
        legacy_manifest.pop("source_mtime_ns")
        manifests[0].write_text(
            json.dumps(legacy_manifest, sort_keys=True),
            encoding="utf-8",
        )
        legacy_plan = plan_family(
            runtime_dir=runtime_dir,
            root_backups=root_backups,
            root_name="root",
            family="wb-finance-canonical",
            reserved_free_bytes=0,
        )
        assert legacy_plan["status"] == "no_change"
        assert legacy_plan["retained"][0]["source_mtime_origin"] == (
            "legacy_archived_at"
        )

        legacy_sidecar = Path(str(verified["source_path"]) + "-shm")
        legacy_sidecar.write_bytes(b"\0" * 32768)
        sidecar_plan = plan_family(
            runtime_dir=runtime_dir,
            root_backups=root_backups,
            root_name="root",
            family="wb-finance-canonical",
            reserved_free_bytes=0,
        )
        assert sidecar_plan["action"] == "remove_verified_owned_sidecars"
        apply_family(
            runtime_dir=runtime_dir,
            root_backups=root_backups,
            root_name="root",
            family="wb-finance-canonical",
            fingerprint=sidecar_plan["fingerprint"],
            deployed_sha=DEPLOYED_SHA,
            reserved_free_bytes=0,
        )
        assert not legacy_sidecar.exists()

        # Build two verified generations again, then emulate a crash after the
        # first exact unlink. The audit-bound apply must resume and reconcile.
        _seed_sqlite(finance / "third.sqlite3", "third")
        _apply_current(
            runtime_dir=runtime_dir,
            root_backups=root_backups,
            root_name="root",
            family="wb-finance-canonical",
        )
        cleanup = plan_family(
            runtime_dir=runtime_dir,
            root_backups=root_backups,
            root_name="root",
            family="wb-finance-canonical",
            reserved_free_bytes=0,
        )
        assert cleanup["action"] == "remove_superseded_verified_generation"
        audit_path = _audit_path(
            runtime_dir=runtime_dir,
            fingerprint=cleanup["fingerprint"],
        )
        _write_audit(
            audit_path,
            {
                "contract_name": cleanup["contract_name"],
                "fingerprint": cleanup["fingerprint"],
                "status": "applying",
                "deployed_sha": DEPLOYED_SHA,
                "started_at": "2026-07-27T00:00:00Z",
                "plan": cleanup,
            },
        )
        Path(cleanup["target_identities"][0]["path"]).unlink()
        resumed = apply_family(
            runtime_dir=runtime_dir,
            root_backups=root_backups,
            root_name="root",
            family="wb-finance-canonical",
            fingerprint=cleanup["fingerprint"],
            deployed_sha=DEPLOYED_SHA,
            reserved_free_bytes=0,
        )
        assert resumed["status"] == "applied"
        repeated = apply_family(
            runtime_dir=runtime_dir,
            root_backups=root_backups,
            root_name="root",
            family="wb-finance-canonical",
            fingerprint=cleanup["fingerprint"],
            deployed_sha=DEPLOYED_SHA,
            reserved_free_bytes=0,
        )
        assert repeated["idempotent"] and not repeated["applied"]
        assert foreign.is_file()

        corrupt = root_backups / "ads-historical"
        corrupt.mkdir()
        (corrupt / "broken.sqlite3.zst").write_bytes(b"not-zstd")
        corrupt_plan = plan_family(
            runtime_dir=runtime_dir,
            root_backups=root_backups,
            root_name="root",
            family="ads-historical",
        )
        assert corrupt_plan["status"] == "critical_stop"
        # A corrupt independent family does not block the already-clean family.
        assert (
            plan_family(
                runtime_dir=runtime_dir,
                root_backups=root_backups,
                root_name="root",
                family="wb-finance-canonical",
            )["status"]
            == "no_change"
        )

        unlisted = root_backups / "foreign-family"
        unlisted.mkdir()
        (unlisted / "do-not-touch").write_text("stable", encoding="utf-8")
        view = inventory(
            runtime_dir=runtime_dir,
            root_backups=root_backups,
        )
        foreign_row = next(
            item for item in view["families"]
            if item["family"] == "foreign-family"
        )
        assert foreign_row["classification"] == "foreign_non_target"
        try:
            plan_family(
                runtime_dir=runtime_dir,
                root_backups=root_backups,
                root_name="root",
                family="foreign-family",
            )
        except SanitationError as exc:
            assert "allowlist" in str(exc)
        else:
            raise AssertionError("unlisted family entered sanitation plan")
        assert json.loads(audit_path.read_text(encoding="utf-8"))["status"] == "applied"

    print("storage_recovery_sanitation_smoke: ok")


if __name__ == "__main__":
    run()
