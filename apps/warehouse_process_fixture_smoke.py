"""Existing file-lock exclusion and crash release; not future B2 durability."""

from pathlib import Path
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ci.fixture_process import checkpoint, fixture_process
from packages.application.warehouse_functional_lock import (
    WarehouseFunctionalBusyError,
    warehouse_functional_job_lock,
    warehouse_functional_write_lock,
)

LOCKS = {"job": warehouse_functional_job_lock, "write": warehouse_functional_write_lock}


def hold_lock(channel, runtime_dir, kind):
    with LOCKS[kind](runtime_dir, blocking=False):
        if kind == "write":
            with warehouse_functional_write_lock(runtime_dir, blocking=False) as evidence:
                assert evidence["reentrant"] == 1.0
        checkpoint(channel, "held")


def main():
    with TemporaryDirectory(prefix="warehouse-process-check-") as temporary:
        root = Path(temporary)
        for kind, lock in LOCKS.items():
            for crash in (False, True):
                with fixture_process(hold_lock, root, kind) as child:
                    child.wait("held")
                    try:
                        with lock(root, blocking=False):
                            raise AssertionError(f"second process acquired {kind} lock")
                    except WarehouseFunctionalBusyError:
                        pass
                    # Separate admission and writer locks already exist. This
                    # does not prove that HTTP uses the correct admission yet.
                    other = LOCKS["write" if kind == "job" else "job"]
                    with other(root, blocking=False):
                        pass
                    if crash:
                        child.crash()
                    else:
                        child.release("held")
                        child.finish()
                with lock(root, blocking=False):
                    pass
    print("warehouse_process_fixture_smoke: OK (two processes, release and actual termination)")


if __name__ == "__main__":
    main()
