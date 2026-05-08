"""Run due sheet_vitrina_v1 feedback auto-complaint schedules.

This is a repo-owned systemd entrypoint. Business times live in runtime state;
the timer only invokes this idempotent due-check.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", default="/opt/wb-core-runtime/state")
    args = parser.parse_args()
    entrypoint = RegistryUploadHttpEntrypoint(runtime_dir=Path(args.runtime_dir).expanduser())
    result = entrypoint.feedbacks_auto_complaints_block.run_due_schedules_sync()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
