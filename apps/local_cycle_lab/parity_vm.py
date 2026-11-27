"""Run manual dispatch, source rejection and in-place stage recovery in the private Linux lab."""

import argparse
from datetime import datetime, timedelta
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from uuid import uuid4

from .full_history_vm import guest, sync_source, ROOT
from .state import atomic_json

APP = "/opt/wb-core-runtime/app"
STATE = "/opt/wb-core-runtime/state"
CASE = "/var/lib/wbc-lab/parity-case"
HISTORY = "/mnt/wb-core-extra100/web-vitrina-history"
STORAGE = "artifacts/registry_upload_http_entrypoint/root_storage_policy_v1.json"
CONTRACT = "artifacts/registry_upload_http_entrypoint/input/web_vitrina_history_runtime.json"
UNITS = "/home/wbc-lab/.config/systemd/user"


def run(fixture, output, stage):
    lock = "/var/lib/wbc-lab/.parity-suite-active"
    guest("sudo", "-u", "wbc-lab", "mkdir", lock)
    try:
        run_owned(fixture, output, stage)
    finally:
        # A surviving unit tree requires explicit cleanup before the next invocation.
        guest("sudo", "-u", "wbc-lab", "test", "!", "-e", UNITS)
        guest("sudo", "-u", "wbc-lab", "rmdir", lock)


def run_owned(fixture, output, stage):
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    name = "parity-" + uuid4().hex
    root = "/var/lib/wbc-lab/runs/" + name
    histories = "/mnt/wb-core-extra100/" + name
    as_lab = ("sudo", "-u", "wbc-lab")

    def read(path):
        return json.loads(guest(*as_lab, "cat", path, capture_output=True, text=True).stdout)

    def mkdir(*paths):
        guest("sudo", "install", "-d", "-m", "700", "-o", "wbc-lab", "-g", "wbc-lab", *paths)

    source = "/opt/wbc-" + name
    guest("sudo", "mkdir", "-m", "755", source)
    with TemporaryDirectory() as temporary:
        sync_source(source, Path(temporary) / "source.tgz")
    guest("sudo", "chown", "-R", "root:root", source)
    uid = guest("id", "-u", "wbc-lab", text=True, capture_output=True).stdout.strip()
    # This VM account, user manager and unit tree belong exclusively to the isolated bench.
    guest("sudo", "systemctl", "start", "user@" + uid + ".service")
    guest(*as_lab, "test", "!", "-e", UNITS)
    mkdir(
        root,
        histories,
        root + "/seed",
        root + "/seed/state",
        histories + "/seed",
        histories + "/seed/backup",
        histories + "/seed/history",
    )
    guest("sudo", "mkdir", "-p", APP, STATE, CASE, HISTORY)
    contract = json.loads((ROOT / CONTRACT).read_bytes())
    contract["mount"] = guest(
        "findmnt",
        "-n",
        "-T",
        "/mnt/wb-core-extra100",
        "-o",
        "TARGET,SOURCE,FSTYPE,OPTIONS",
        text=True,
        capture_output=True,
    ).stdout.strip()

    def write_file(path, data):
        guest("sudo", "tee", path, input=data, stdout=subprocess.DEVNULL)

    write_file(root + "/contract.json", json.dumps(contract).encode())
    device = guest(
        "findmnt", "-n", "-T", "/mnt/wb-core-extra100", "-o", "SOURCE", text=True, capture_output=True
    ).stdout.strip()
    uuid = guest(
        "sudo", "blkid", "-s", "UUID", "-o", "value", device, text=True, capture_output=True
    ).stdout.strip()
    policy = json.loads((ROOT / STORAGE).read_bytes())
    policy["storage_registry"]["filesystems"]["backup"].update(source=device, filesystem_uuid=uuid)
    write_file(root + "/storage.json", json.dumps(policy).encode())
    identity = read(source + "/local-lab-source.json")
    guest("sudo", "install", "-m", "600", "-o", "wbc-lab", "/dev/null", root + "/fixture.json")
    with fixture.open("rb") as stream:
        guest(*as_lab, "tee", root + "/fixture.json", stdin=stream, stdout=subprocess.DEVNULL)

    def unit(case, action):
        clock = read_seed_day + " 08:00:00 UTC"
        if action != "seed":
            previous = {
                "recover": "interruption.json",
                "reject-post": "interruption.json",
                "repeat": "recovered.json",
            }.get(action, "seed-clock.json")
            saved = read(root + "/" + case + "/evidence/" + previous)
            clock = (datetime.fromisoformat(saved["observed_at"]) + timedelta(seconds=1)).strftime(
                "%Y-%m-%d %H:%M:%S UTC"
            )
        command = (
            "sudo",
            "systemd-run",
            "--quiet",
            "--wait",
            "--pipe",
            "--collect",
            "--uid=wbc-lab",
            "--gid=wbc-lab",
            "--working-directory=" + APP,
            "--property=PrivateNetwork=yes",
            "--property=RuntimeMaxSec=5min",
            "--property=NoNewPrivileges=yes",
            "--property=CapabilityBoundingSet=",
            "--property=PrivateTmp=yes",
            "--property=ProtectSystem=strict",
            "--property=ReadWritePaths=/mnt/wb-core-extra100",
            *(
                ["--property=ReadWritePaths=/home/wbc-lab"]
                if action == "seed"
                else ["--property=ProtectHome=yes", "--property=InaccessiblePaths=/run/user/" + uid]
            ),
            "--property=BindReadOnlyPaths=" + source + ":" + APP,
            "--property=BindReadOnlyPaths=" + root + "/contract.json:" + APP + "/" + CONTRACT,
            "--property=BindReadOnlyPaths=" + root + "/storage.json:" + APP + "/" + STORAGE,
            "--property=BindReadOnlyPaths=" + root + "/fixture.json:/var/lib/wbc-lab/parity-input.json",
            "--property=BindPaths=" + root + "/" + case + ":" + CASE,
            "--property=BindPaths=" + root + "/" + case + "/state:" + STATE,
            "--property=BindPaths=" + histories + "/" + case + "/history:" + HISTORY,
            "--property=BindPaths=" + histories + "/" + case + "/backup:" + STATE + "/backups",
            "--setenv=HOME=/home/wbc-lab",
            "--setenv=XDG_RUNTIME_DIR=/run/user/" + uid,
            "--setenv=PLAYWRIGHT_BROWSERS_PATH=/opt/wbc-lab-browsers",
            "--setenv=PYTHONUNBUFFERED=1",
            "/usr/bin/datefudge",
            clock,
            "/opt/wbc-lab-venv/bin/python",
            "-m",
            "apps.local_cycle_lab.parity_case",
            action,
            *(
                ["--interrupt-after-warehouse-post"]
                if stage == "warehouse"
                else ["--interrupt-before", stage]
            ),
        )
        guest(*command)

    read_seed_day = json.loads((ROOT / "apps/local_cycle_lab/scenario.json").read_bytes())["today"]
    try:
        unit("seed", "seed")
        for case in ("control", "negative", "crash"):
            guest("sudo", "cp", "-a", "--reflink=auto", root + "/seed", root + "/" + case)
            guest("sudo", "cp", "-a", "--reflink=auto", histories + "/seed", histories + "/" + case)
        for case in ("control", "negative"):
            unit(case, case)
        try:
            unit("crash", "crash")
        except subprocess.CalledProcessError as exc:
            if exc.returncode != 84:
                raise
        else:
            raise RuntimeError("Interrupted daemon unexpectedly survived")
        if stage == "warehouse":
            unit("crash", "reject-post")
        unit("crash", "recover")
        control = read(root + "/control/evidence/uninterrupted.json")
        repaired = read(root + "/crash/evidence/recovered.json")

        # Compare all economic operands, including document identity, on the two branches.
        # The physical processes can stamp valuation proof provenance at different instants.
        def operands(business):
            row = business["accounting_row"]
            return {
                **business,
                "accounting_row": {
                    **row,
                    "documents": [
                        {
                            k: d[k]
                            for k in (
                                "document_id",
                                "kind",
                                "facility_id",
                                "nm_id",
                                "quantity",
                                "capital_rub",
                            )
                        }
                        for d in row["documents"]
                    ],
                },
            }

        fields = ("ready_cells", "history_cells", "vitrina_cells", "history_dates")
        if (
            control["history_values"] != repaired["history_values"]
            or control["tables"] != repaired["tables"]
            or operands(control["business"]) != operands(repaired["business"])
            or any(control["publication"][key] != repaired["publication"][key] for key in fields)
        ):
            raise ValueError("Recovered business values or publication differ from uninterrupted control")
        if (
            stage == "warehouse"
            and control["warehouse_source_calls"] != repaired["warehouse_post"]["source_calls"]
        ):
            raise ValueError("Repaired warehouse source calls differ from its uninterrupted handler")
        unit("crash", "repeat")
        repeated = read(root + "/crash/evidence/repeat.json")
        if (
            repeated["job"] in (repaired["recovery_job"], repaired["original_job"])
            or operands(repeated["business"]) != operands(repaired["business"])
            or repeated["publication"]["ready_cells"] != repaired["publication"]["ready_cells"]
        ):
            raise ValueError("Second manual cycle changed the expense")
        result = dict(
            status="passed",
            guest_suite=name,
            tested_source=identity,
            profile=read(root + "/seed/evidence/profile.json"),
            captured_source=read(root + "/seed/evidence/capture-result.json"),
            negative=read(root + "/negative/evidence/rejected.json"),
            control=control,
            recovered=repaired,
            repeat=repeated,
        )
        if stage == "warehouse":
            result["rejected_post"] = read(root + "/crash/evidence/rejected-post.json")
        atomic_json(output / "verification.json", result)
    finally:
        # Our user-unit directory was absent at entry. Its contents are generated only by this suite.
        owned = guest(
            *as_lab, "sh", "-c", 'test ! -d "$1" || echo present', "sh", UNITS, capture_output=True, text=True
        ).stdout.strip()
        if owned:
            userctl = (*as_lab, "env", "XDG_RUNTIME_DIR=/run/user/" + uid, "systemctl", "--user")
            timer = "wb-core-warehouse-functional-sync.timer"
            guest(*userctl, "disable", "--now", timer)
            state = guest(
                *userctl, "show", timer, "-p", "ActiveState,UnitFileState", text=True, capture_output=True
            ).stdout
            if set(state.splitlines()) != {"ActiveState=inactive", "UnitFileState=disabled"}:
                raise RuntimeError("Could not prove isolated timer stopped; suite lock retained")
            guest(*as_lab, "mv", UNITS, root + "/timer-fixtures")
            guest(*userctl, "daemon-reload")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("private_fixture", type=Path)
    parser.add_argument("output", type=Path)
    from .parity_case import add_interruption_argument

    add_interruption_argument(parser)
    args = parser.parse_args()
    run(args.private_fixture, args.output, args.stage)
