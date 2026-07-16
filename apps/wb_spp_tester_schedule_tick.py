"""Run the idempotent daily SPP due-check with authenticated-buyer preflight.

The shared application block records an invalid buyer session as a no-write
scheduled skip and never falls back to the anonymous control price.
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
    result = entrypoint.spp_tester_block.run_due_schedule_tick()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
