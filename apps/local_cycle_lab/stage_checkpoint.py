"""Immutable inputs captured by the single writer immediately before a native stage."""
from contextlib import closing, contextmanager, ExitStack
import hashlib
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
from unittest.mock import patch

from .full_cycle import SCENARIO, expect
from .full_history import MOUNT
from .state import atomic_json, read_json

CASE = Path("/var/lib/wbc-lab/stage-case")
CONTROL = CASE / "control"
REFERENCE = Path("/var/lib/wbc-lab/stage-input")
SUPPORTED = ("warehouse", "rolling14")
FORMAT = "wbc-local-stage-input-v1"
SOURCE_ID = Path(__file__).resolve().parents[2] / "local-lab-source.json"


def history_root():
    suffix = hashlib.sha256(str(CONTROL / "runtime").encode()).hexdigest()[:16]
    return MOUNT / "local-history" / suffix


def file_hash(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def inventory(root):
    files = {}
    for path in sorted(root.rglob("*")):
        if path.is_dir() and not path.is_symlink():
            continue
        expect(path.is_file() and not path.is_symlink(), f"Unexpected checkpoint entry: {path}")
        files[str(path.relative_to(root))] = file_hash(path)
    return files


def copy_database(source, target):
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as src:
        src.execute("PRAGMA query_only=ON")
        with closing(sqlite3.connect(target)) as dest:
            src.backup(dest)
            expect(dest.execute("PRAGMA quick_check").fetchall() == [("ok",)], "Invalid database copy")
    return target


def copy_inputs(source, destination):
    def copy_file(src, dst):
        path = Path(src)
        # Recovery and cutover backups are sealed artifacts with byte-level manifests.
        sealed = path.relative_to(source).parts[0] in {"backups", "lab-bootstrap-backups"}
        if path.suffix == ".sqlite3" and not sealed:
            return copy_database(path, dst)
        return shutil.copy2(src, dst)

    def ignore(parent, names):
        for name in names:
            path = Path(parent) / name
            expect(not path.is_symlink() and (path.is_file() or path.is_dir()), f"Unsupported input: {path}")
        # Each SQLite backup includes its committed WAL. Never restore the source WAL locks.
        return {name for name in names if name.endswith((".sqlite3-shm", ".sqlite3-wal", ".sqlite3-journal"))}

    shutil.copytree(source, destination, copy_function=copy_file, ignore=ignore)


def save_before(work, stage, receipt):
    from packages.application.sheet_vitrina_v1_cycle import STAGES
    prefix = list(STAGES[:STAGES.index(stage)])
    expect(receipt["current_stage"] == stage and
           [s["stage"] for s in receipt["stages"] if s["status"] in ("complete", "degraded")] == prefix,
           "Unexpected native stage boundary")
    point_root = work / "checkpoints"
    point_root.mkdir(mode=0o700, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=point_root) as temporary:
        point = Path(temporary) / "snapshot"
        point.mkdir()
        copy_inputs(work / "runtime", point / "runtime")
        # History objects are content-addressed SQLite files. Preserve their exact bytes.
        # The admitted cycle owns this private candidate; there is no independent writer.
        inventory(history_root())
        shutil.copytree(history_root(), point / "history")
        for name in ("input.json", "opening.json", "before.json"):
            shutil.copy2(work / name, point / name)
        shutil.copy2(SCENARIO, point / "scenario.json")
        atomic_json(point / "source-receipt.json", receipt)
        current = next(s for s in receipt["stages"] if s["stage"] == stage)
        manifest = dict(schema=FORMAT, stage=stage, captured_at=current["started_at"],
                        source_cycle=receipt["cycle_id"], source_code=read_json(SOURCE_ID), runtime=str(work / "runtime"),
                        candidate=str(history_root()), files=inventory(point))
        atomic_json(point / "manifest.json", manifest)
        os.rename(point, point_root / stage)


@contextmanager
def recording(work, *, interrupt_after=None):
    from packages.application.sheet_vitrina_v1_cycle import CycleReceiptStore
    write = CycleReceiptStore.write
    saved = set()

    def record(store, receipt):
        result = write(store, receipt)
        stage = receipt.get("current_stage")
        if (receipt.get("request_key") == "manual-cycle:local-expense-1" and
                receipt.get("status") == "running" and stage in SUPPORTED and stage not in saved):
            save_before(work, stage, receipt)
            saved.add(stage)
        return result

    with ExitStack() as patches:
        patches.enter_context(patch.object(CycleReceiptStore, "write", record))
        if interrupt_after:
            from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
            method = {"warehouse": "_cycle_warehouse", "rolling14": "_cycle_history"}[interrupt_after]
            native = getattr(RegistryUploadHttpEntrypoint, method)
            def interrupted(*args, **kwargs):
                native(*args, **kwargs)
                raise RuntimeError("Deliberate local failure after native effects, before the stage receipt")
            patches.enter_context(patch.object(RegistryUploadHttpEntrypoint, method, interrupted))
        yield


def restore(stage):
    point = REFERENCE / "control" / "checkpoints" / stage
    manifest = read_json(point / "manifest.json")
    expect(manifest["schema"] == FORMAT and manifest["stage"] == stage and
           manifest["runtime"] == str(CONTROL / "runtime") and manifest["candidate"] == str(history_root()),
           "Checkpoint scope differs")
    actual = inventory(point)
    actual.pop("manifest.json")
    expect(actual == manifest["files"], "Checkpoint checksum or file set differs")
    expect(file_hash(SCENARIO) == actual["scenario.json"], "Scenario differs from the recorded input")
    CONTROL.mkdir(mode=0o700, exist_ok=False)
    # The original PID and descriptors cannot be restored. Preserve the original
    # API job markers in the checkpoint; the admitted new process owns fresh markers.
    shutil.copytree(point / "runtime", CONTROL / "runtime",
                    ignore=lambda parent, names: {"finished-snapshot-api-jobs"} if Path(parent) == point / "runtime" else set())
    shutil.copytree(point / "history", history_root())
    return point, manifest
