#!/usr/bin/env python3
"""Dependency-isolated smoke for the WBC0027 capsule target workflow step."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps import wbc0027_incident_capsule_target as target_module  # noqa: E402
from apps import wbc0027_incident_capsule_workflow as workflow_module  # noqa: E402


KNOWN_HOSTS = "89.191.226.88 ssh-ed25519 c3ludGhldGljLWtleQ=="
PRIVATE_KEY = "synthetic-private-key"


def _expect_blocked(root: Path, *, label: str, target_file: Path) -> None:
    blocked = False
    try:
        workflow_module.materialize_ssh_transport(
            target_file=target_file,
            output_directory=root / f"capsule-ssh-{label}",
            private_key=PRIVATE_KEY,
            known_hosts=KNOWN_HOSTS,
        )
    except workflow_module.ApplyError as exc:
        blocked = "capsule canonical target is invalid" in str(exc)
    assert blocked, label


def main() -> int:
    assert "openpyxl" not in sys.modules
    with TemporaryDirectory(prefix="wbc0027-target-smoke-") as raw_root:
        root = Path(raw_root)
        binding = workflow_module.materialize_ssh_transport(
            target_file=target_module.DEFAULT_TARGET_FILE,
            output_directory=root / "capsule-ssh",
            private_key=PRIVATE_KEY,
            known_hosts=KNOWN_HOSTS,
        )
        assert binding["target_id"] == target_module.CANONICAL_TARGET_ID
        assert binding["host_name"] == "89.191.226.88"
        assert binding["user"] == target_module.CANONICAL_SSH_USER
        assert binding["source_ssh_destination"] == target_module.CANONICAL_SSH_DESTINATION
        config = Path(binding["ssh_config"]).read_text(encoding="utf-8")
        assert "    HostName 89.191.226.88\n" in config
        assert "    User root\n" in config
        assert "    StrictHostKeyChecking yes\n" in config
        assert f"    HostKeyAlias {binding['host_name']}\n" in config
        assert "    CheckHostIP yes\n" in config
        assert KNOWN_HOSTS in Path(binding["known_hosts_file"]).read_text(encoding="utf-8")

        missing_known_hosts = False
        try:
            workflow_module.materialize_ssh_transport(
                target_file=target_module.DEFAULT_TARGET_FILE,
                output_directory=root / "capsule-ssh-missing-known-hosts",
                private_key=PRIVATE_KEY,
                known_hosts="",
            )
        except workflow_module.ApplyError as exc:
            missing_known_hosts = "SSH credentials are missing" in str(exc)
        assert missing_known_hosts

        canonical = json.loads(target_module.DEFAULT_TARGET_FILE.read_text(encoding="utf-8"))
        invalid_payloads = {
            "missing-host": {**canonical, "host_ip": ""},
            "foreign-host": {**canonical, "host_ip": "203.0.113.10"},
            "foreign-target": {**canonical, "target_id": "foreign-target"},
        }
        for label, payload in invalid_payloads.items():
            path = root / f"target-{label}.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            _expect_blocked(root, label=label, target_file=path)

        malformed = root / "target-malformed.json"
        malformed.write_text("{", encoding="utf-8")
        _expect_blocked(root, label="malformed", target_file=malformed)
        _expect_blocked(root, label="missing-file", target_file=root / "missing.json")
    assert "openpyxl" not in sys.modules
    print("wbc0027_incident_capsule_target_smoke: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
