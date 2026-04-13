"""Локальный live runner для HTTP entrypoint registry upload."""

import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (
    build_registry_upload_http_server,
    load_registry_upload_http_entrypoint_config,
)
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint


def main() -> None:
    config = load_registry_upload_http_entrypoint_config()
    activated_at_override = os.environ.get("REGISTRY_UPLOAD_ACTIVATED_AT_OVERRIDE", "").strip()
    entrypoint = RegistryUploadHttpEntrypoint(
        runtime_dir=config.runtime_dir,
        activated_at_factory=(
            (lambda: activated_at_override)
            if activated_at_override
            else None
        ),
    )
    server = build_registry_upload_http_server(config, entrypoint=entrypoint)
    host, port = server.server_address
    print(f"registry upload http entrypoint: http://{host}:{port}{config.upload_path}")
    print(f"runtime dir: {config.runtime_dir}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
