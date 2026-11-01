"""Isolate the saved monitor read from the serial operational HTTP queue."""
from http.server import HTTPServer
import threading
from urllib.parse import urlsplit

from packages.adapters.registry_upload_http_entrypoint import DEFAULT_STOCK_MONITOR_PATH
from packages.application.web_vitrina_window_read_context import window_read_context

STOCK_MONITOR_HTTP_PORT = 8780


def start_stock_monitor_contour(primary_server, *, port=STOCK_MONITOR_HTTP_PORT):
    primary_handler = primary_server.RequestHandlerClass

    class MonitorReadHandler(primary_handler):
        def do_GET(self):  # noqa: N802
            if urlsplit(self.path).path != DEFAULT_STOCK_MONITOR_PATH:
                self.send_error(404)
                return
            # Normal session and section checks still run in the primary handler.
            # Runtime-user lookup borrows this query-only connection: no schema
            # bootstrap/writer lock, and no unrelated FBS accounting store read.
            runtime = self.runtime_entrypoint.runtime
            with window_read_context(runtime.db_path):
                super().do_GET()

        def _reject_mutation(self):
            self.send_error(405)

        do_POST = do_PATCH = do_DELETE = do_PUT = do_HEAD = do_OPTIONS = _reject_mutation

    # A plain server deliberately owns neither the primary service wakeup nor
    # its shared window service. Closing it must only close this listener.
    server = HTTPServer(('127.0.0.1', port), MonitorReadHandler)
    thread = threading.Thread(target=server.serve_forever, name='stock-monitor-read-http', daemon=True)
    try:
        thread.start()
    except BaseException:
        server.server_close()
        raise
    return server, thread
