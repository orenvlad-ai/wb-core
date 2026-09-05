#!/usr/bin/env python3
"""Offline behavior checks for the one-submit launcher."""

import json
import subprocess
import sys
from pathlib import Path
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps import production_apply_launcher as launcher


D1 = "sha256:" + "1" * 64
D2 = "sha256:" + "2" * 64


class Fake:
    def __init__(self, *, ambiguous: bool = False) -> None:
        self.state = "not_submitted"
        self.apply_count = 0
        self.ambiguous = ambiguous

    def preview(self, request, operation_id):
        return {"operation_id": operation_id, "target": "test", "scope": {"rows": 1}, "prestate_sha256": D1, "candidate_sha256": D2, "recovery": {"kind": "undo", "id": "r1"}}

    def apply(self, request, operation_id, preview):
        self.apply_count += 1
        self.state = "applied"
        if self.ambiguous:
            raise launcher.AmbiguousSubmit
        return {"operation_id": operation_id, "disposition": "submitted"}

    def readback(self, request, operation_id):
        return {"operation_id": operation_id, "state": self.state}


def main() -> None:
    fake = Fake()
    preview = launcher.execute(action="preview", adapter_name="fake", operation_id="operation-0001", request={}, adapters={"fake": fake})
    assert preview["state"] == "preview" and fake.apply_count == 0
    result = launcher.execute(action="apply", adapter_name="fake", operation_id="operation-0001", request={}, expected_prestate=D1, expected_candidate=D2, adapters={"fake": fake})
    assert result["state"] == "applied" and fake.apply_count == 1
    repeat = launcher.execute(action="apply", adapter_name="fake", operation_id="operation-0001", request={}, expected_prestate=D1, expected_candidate=D2, adapters={"fake": fake})
    assert repeat["state"] == "applied" and fake.apply_count == 1

    ambiguous = Fake(ambiguous=True)
    result = launcher.execute(action="apply", adapter_name="ambiguous", operation_id="operation-0002", request={}, expected_prestate=D1, expected_candidate=D2, adapters={"ambiguous": ambiguous})
    assert result["state"] == "applied" and ambiguous.apply_count == 1

    drift = Fake()
    try:
        launcher.execute(action="apply", adapter_name="fake", operation_id="operation-0003", request={}, expected_prestate=D2, expected_candidate=D2, adapters={"fake": drift})
    except launcher.ApplyError:
        pass
    else:
        raise AssertionError("drift was accepted")
    assert drift.apply_count == 0

    sensitive = launcher.make_receipt(
        action="preview",
        adapter="fake",
        operation_id="operation-0004",
        state="preview",
        preview={
            "target": "production-row-identity",
            "scope": {"seller_warehouse_id": 123456789},
            "prestate_sha256": D1,
            "candidate_sha256": D2,
            "recovery": {"kind": "undo"},
        },
    )
    assert "production-row-identity" not in str(launcher.log_summary(sensitive))
    assert "123456789" not in str(launcher.log_summary(sensitive))

    with tempfile.TemporaryDirectory(prefix="production-apply-cli-") as raw:
        output = Path(raw) / "receipt.json"
        completed = subprocess.run(
            [
                sys.executable,
                "-I",
                str(ROOT / "apps/production_apply_launcher.py"),
                "readback",
                "--adapter",
                "fixture-not-registered",
                "--operation-id",
                "operation-0005",
                "--request-json",
                "{}",
                "--output",
                str(output),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        assert completed.returncode == 2
        cli_receipt = json.loads(output.read_text(encoding="utf-8"))
        assert cli_receipt["reason"] == "adapter-not-registered"

    workflow = (ROOT / ".github/workflows/production-apply.yml").read_text(
        encoding="utf-8"
    )
    assert "APPLY_REQUEST_JSON:" not in workflow
    assert "GITHUB_EVENT_PATH" in workflow
    print("production_apply_launcher_smoke: ok")


if __name__ == "__main__":
    main()
