"""Independent read process. No background jobs or writable initialization."""
import os
import logging
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from packages.adapters.supplier_view_http import build_supplier_view_server
from packages.adapters.registry_upload_http_entrypoint import _web_auth_config

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if not _web_auth_config()["enabled"]:
        raise SystemExit("WebCore authentication is required for supplier views")
    server = build_supplier_view_server(
        Path(os.environ.get("REGISTRY_UPLOAD_RUNTIME_DIR", "/opt/wb-core-runtime/state")),
        port=int(os.environ.get("SUPPLIER_VIEW_HTTP_PORT", "8782")))
    try:
        server.serve_forever()
    finally:
        server.server_close()
