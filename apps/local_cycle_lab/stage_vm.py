"""Host CLI for capturing and replaying a native stage on an isolated local VM."""
import argparse
from datetime import datetime, timedelta
import json
import os
from pathlib import Path, PurePosixPath
import re
import tarfile
from tempfile import NamedTemporaryFile
from uuid import uuid4

from .full_history_vm import guest
from .stage_checkpoint import CASE, REFERENCE, SCENARIO, SUPPORTED


def case_name(value):
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", value):
        raise argparse.ArgumentTypeError("Use a short case name of lowercase letters, numbers, - and _")
    return value


def transfer(action, source, archive):
    # Only checkpoints and control evidence are portable. The live post-run
    # runtime, its locks and process owners are never installed over a case.
    allowed = {"control/checkpoints", "control/after-1.json", "control/cycle-1.json"}
    if action == "save":
        names = guest("sudo", "-u", "wbc-lab", "ls", source + "/control", capture_output=True, text=True).stdout.splitlines()
        members = sorted("control/" + n for n in names if "control/" + n in allowed)
        if "control/checkpoints" not in members:
            raise ValueError("No saved checkpoint")
        with NamedTemporaryFile(dir=archive.parent) as temporary:
            guest("sudo", "-u", "wbc-lab", "tar", "-czf", "-", "-C", source, *members, stdout=temporary)
            os.link(temporary.name, archive)  # Publish the complete archive once, without replacing a prior file.
    else:
        with archive.open("rb") as stream:
            with tarfile.open(fileobj=stream, mode="r:gz") as packed:
                names = set()
                for member in packed:
                    path = PurePosixPath(member.name)
                    if (not (member.isfile() or member.isdir()) or path.is_absolute() or ".." in path.parts
                            or len(path.parts) < 2 or "/".join(path.parts[:2]) not in allowed or path in names):
                        raise ValueError("Unexpected checkpoint archive member")
                    names.add(path)
                if not any(path.name == "manifest.json" and path.parent.name in SUPPORTED for path in names):
                    raise ValueError("No stage manifest in archive")
            stream.seek(0)
            guest("sudo", "-u", "wbc-lab", "mkdir", "-m", "700", source)
            guest("sudo", "-u", "wbc-lab", "tar", "-xzf", "-", "--no-same-owner", "-C", source, stdin=stream)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    full = actions.add_parser("capture", help="Run the full control and save its pre-stage checkpoints")
    full.add_argument("name", type=case_name)
    full.add_argument("--interrupt-after", choices=SUPPORTED, help="Stop after native effects and before the stage can acknowledge success")
    replay = actions.add_parser("replay", help="Run one stage in a new case cloned from a checkpoint")
    replay.add_argument("reference", type=case_name)
    replay.add_argument("name", type=case_name)
    replay.add_argument("--stage", required=True, choices=SUPPORTED)
    for action in ("save", "load"):
        archive = actions.add_parser(action, help="Transfer a portable checkpoint archive between the Mac and the VM")
        archive.add_argument("name", type=case_name)
        archive.add_argument("archive", type=Path)
    args = parser.parse_args()
    runs = "/var/lib/wbc-lab/runs/stage-"
    if args.action in ("save", "load"):
        transfer(args.action, runs + args.name, args.archive)
        return
    histories = "/mnt/wb-core-extra100/stage-runs"
    source = runs + args.name
    target = histories + "/" + args.name
    clock = json.loads(SCENARIO.read_text())["today"] + " 08:00:00 UTC"
    paths = [f"--property=BindPaths={source}:{CASE}", f"--property=BindPaths={target}:/mnt/wb-core-extra100/local-history"]
    command = [args.action]
    if args.action == "replay":
        ref = runs + args.reference
        raw = guest("sudo", "-u", "wbc-lab", "cat", ref + "/control/checkpoints/" + args.stage + "/manifest.json",
                    capture_output=True, text=True).stdout
        captured = datetime.fromisoformat(json.loads(raw)["captured_at"].replace("Z", "+00:00"))
        clock = (captured + timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S UTC")
        paths.append(f"--property=BindReadOnlyPaths={ref}:{REFERENCE}")
        command += ["--stage", args.stage]
    elif args.interrupt_after:
        command += ["--interrupt-after", args.interrupt_after]
    # These names are exclusively reserved for the lab. mkdir without -p
    # on each case refuses reuse, even after an interrupted attempt.
    guest("sudo", "-u", "wbc-lab", "mkdir", "-p", str(CASE), str(REFERENCE))
    guest("sudo", "install", "-d", "-m", "700", "-o", "wbc-lab", "-g", "wbc-lab", histories)
    guest("sudo", "-u", "wbc-lab", "mkdir", "-m", "700", source)
    guest("sudo", "-u", "wbc-lab", "mkdir", "-m", "700", target)
    guest("sudo", "systemd-run", "--quiet", "--wait", "--pipe", "--collect",
          "--unit=wbc-stage-" + uuid4().hex, "--uid=wbc-lab", "--gid=wbc-lab", "--working-directory=/opt/wbc-lab",
          "--property=PrivateNetwork=yes", "--property=NoNewPrivileges=yes", "--property=CapabilityBoundingSet=",
          "--property=ProtectSystem=strict", "--property=ProtectHome=yes", "--property=PrivateTmp=yes", *paths,
          "--setenv=PLAYWRIGHT_BROWSERS_PATH=/opt/wbc-lab-browsers", "--setenv=PYTHONUNBUFFERED=1",
          "/usr/bin/datefudge", clock, "/opt/wbc-lab-venv/bin/python", "-m", "apps.local_cycle_lab.stage_replay",
          *command)


if __name__ == "__main__":
    main()
