#!/usr/bin/env python3
"""Offline checks for the exact-SHA mapping-evidence server entrypoint."""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps import wb_fbs_mapping_evidence as entrypoint  # noqa: E402
from packages.application.wb_fbs_mapping_evidence import (  # noqa: E402
    WbFbsMappingEvidenceError,
)


SHA = "a" * 40


class FakeManifest:
    manifest_sha256 = "sha256:" + "b" * 64


class FakeGeneration:
    generation_id = "operational-fixture"
    generation_epoch = "fixture-epoch"


class FakeRegistry:
    def __init__(self, root: Path) -> None:
        self.root = root

    def load(self, *, require_files: bool):
        assert require_files is True
        return FakeManifest()

    def generation(self, role: str, *, manifest):
        assert role == "operational" and isinstance(manifest, FakeManifest)
        return FakeGeneration()

    def resolve(self, role: str, *, manifest):
        assert role == "operational" and isinstance(manifest, FakeManifest)
        return self.root / "operational.sqlite3"


class FakeService:
    instances = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.instances.append(self)

    def preview(self, request, operation_id):
        return {"route": "preview", "request": dict(request), "operation_id": operation_id}

    def apply(
        self,
        request,
        operation_id,
        *,
        expected_prestate,
        expected_candidate,
    ):
        return {
            "route": "apply",
            "request": dict(request),
            "operation_id": operation_id,
            "expected_prestate": expected_prestate,
            "expected_candidate": expected_candidate,
        }

    def readback(self, request, operation_id):
        return {"route": "readback", "request": dict(request), "operation_id": operation_id}


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="wb-fbs-evidence-entrypoint-") as raw:
        root = Path(raw)
        (root / ".wb-core-runtime-sha").write_text(SHA + "\n", encoding="utf-8")
        env_reads = []
        source = object()
        entrypoint.ROOT = root
        entrypoint.StoreRegistry = FakeRegistry
        entrypoint.WbFbsMappingEvidenceUpgrade = FakeService
        entrypoint._load_env_file = lambda path: env_reads.append(path)
        entrypoint.HttpBackedWbFbsOrdersSource = lambda: source

        base = {
            "operation_id": "operation-fixture-entrypoint",
            "request": {"mapping_id": "mapping-fixture"},
            "expected_prestate": "sha256:" + "1" * 64,
            "expected_candidate": "sha256:" + "2" * 64,
            "expected_runtime_sha": SHA,
            "actor": "fixture",
        }
        readback = entrypoint.execute(
            {**base, "action": "readback"},
            runtime_dir=root / "state",
            env_file=root / "runtime.env",
        )
        assert readback["route"] == "readback"
        assert env_reads == []
        assert FakeService.instances[-1].kwargs["source"] is None

        preview = entrypoint.execute(
            {**base, "action": "preview"},
            runtime_dir=root / "state",
            env_file=root / "runtime.env",
        )
        assert preview["route"] == "preview"
        assert env_reads == [(root / "runtime.env").resolve()]
        assert FakeService.instances[-1].kwargs["source"] is source
        assert FakeService.instances[-1].kwargs["storage_identity"] == {
            "generation_id": "operational-fixture",
            "generation_epoch": "fixture-epoch",
            "manifest_sha256": FakeManifest.manifest_sha256,
        }

        applied = entrypoint.execute(
            {**base, "action": "apply"},
            runtime_dir=root / "state",
            env_file=root / "runtime.env",
        )
        assert applied["route"] == "apply"
        assert applied["expected_prestate"] == base["expected_prestate"]
        assert applied["expected_candidate"] == base["expected_candidate"]

        try:
            entrypoint.execute(
                {**base, "action": "readback", "expected_runtime_sha": "c" * 40},
                runtime_dir=root / "state",
                env_file=root / "runtime.env",
            )
        except WbFbsMappingEvidenceError as exc:
            assert exc.code == "deployed_runtime_sha_mismatch"
        else:
            raise AssertionError("runtime SHA mismatch was accepted")
    print("wb fbs mapping evidence entrypoint smoke: ok")


if __name__ == "__main__":
    main()
