"""Targeted smoke-check for retrying zero-filled current-day search + seller snapshots."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
import threading
from urllib import parse as urllib_parse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.web_source_current_sync import (  # noqa: E402
    ShellBackedWebSourceCurrentSync,
    WebSourceCurrentSyncConfig,
)

TARGET_DATE = "2026-04-19"
REQUESTED_NM_IDS = [210183919, 210184534]


class _NoopSessionSync(ShellBackedWebSourceCurrentSync):
    def _ensure_seller_portal_session_ready(self) -> None:
        return


def main() -> None:
    with TemporaryDirectory(prefix="web-source-current-sync-zero-") as tmp:
        tmp_path = Path(tmp)
        state_path = tmp_path / "state.json"
        _write_state(
            state_path,
            {
                "search_valid_dates": [],
                "search_zero_dates": [TARGET_DATE],
                "seller_valid_dates": [],
                "seller_zero_dates": [TARGET_DATE],
                "calls": [],
            },
        )

        with _MockSellerosApi(state_path=state_path, requested_nm_ids=REQUESTED_NM_IDS) as api:
            bot_dir = _build_fake_bot_dir(tmp_path / "wb-web-bot", state_path=state_path)
            ai_dir = _build_fake_ai_dir(tmp_path / "wb-ai", state_path=state_path)
            sync = _NoopSessionSync(
                WebSourceCurrentSyncConfig(
                    mode="force",
                    wb_web_bot_dir=bot_dir,
                    wb_ai_dir=ai_dir,
                    api_base_url=api.base_url,
                    timeout_sec=30,
                    canonical_supplier_id="",
                    canonical_supplier_label="",
                )
            )

            sync.ensure_snapshot(TARGET_DATE)
            state = _read_state(state_path)
            if state["calls"] != [
                {"kind": "bot.runner_day", "date": TARGET_DATE},
                {"kind": "run_web_source_handoff.py", "dataset": "search-analytics", "date": TARGET_DATE},
                {"kind": "bot.runner_sales_funnel_day", "date": TARGET_DATE},
                {"kind": "run_web_source_handoff.py", "dataset": "sales-funnel", "date": TARGET_DATE},
            ]:
                raise AssertionError(f"current sync must retry zero-filled search + seller snapshots, got calls={state['calls']}")

            search_payload = _get_json(f"{api.base_url}/v1/search-analytics/snapshot?date_to={TARGET_DATE}")
            first_search_item = search_payload["items"][0]
            if (
                first_search_item["views_current"] <= 0
                and first_search_item["ctr_current"] <= 0
                and first_search_item["orders_current"] <= 0
            ):
                raise AssertionError("search_analytics payload must be repaired to non-zero values after retry")
            seller_payload = _get_json(f"{api.base_url}/v1/sales-funnel/daily?date={TARGET_DATE}")
            first_item = seller_payload["items"][0]
            if first_item["view_count"] <= 0 or first_item["open_card_count"] <= 0:
                raise AssertionError("seller_funnel payload must be repaired to non-zero values after retry")

            sync.ensure_snapshot(TARGET_DATE)
            second_state = _read_state(state_path)
            if second_state["calls"] != state["calls"]:
                raise AssertionError("usable search/seller snapshots must not trigger duplicate retries")

            print(f"current_retry: ok -> {state['calls']}")
            print(f"search_payload: ok -> {first_search_item['views_current']} / {first_search_item['orders_current']}")
            print(f"seller_payload: ok -> {first_item['view_count']} / {first_item['open_card_count']}")
            print("smoke-check passed")


def _build_fake_bot_dir(root: Path, *, state_path: Path) -> Path:
    python_path = Path(sys.executable).resolve()
    (root / "bot").mkdir(parents=True, exist_ok=True)
    (root / "venv" / "bin").mkdir(parents=True, exist_ok=True)
    (root / "bot" / "__init__.py").write_text("", encoding="utf-8")
    (root / "venv" / "bin" / "python").symlink_to(python_path)
    _write_script(
        root / "bot" / "runner_day.py",
        f"""
import json
import sys
from pathlib import Path

STATE_PATH = Path({json.dumps(str(state_path))})

state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
target_date = sys.argv[1]
state.setdefault("calls", []).append({{"kind": "bot.runner_day", "date": target_date}})
state["search_zero_dates"] = [value for value in state.get("search_zero_dates", []) if value != target_date]
if target_date not in state.get("search_valid_dates", []):
    state.setdefault("search_valid_dates", []).append(target_date)
STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
""".strip()
        + "\n",
    )
    _write_script(
        root / "bot" / "runner_sales_funnel_day.py",
        f"""
import json
import sys
from pathlib import Path

STATE_PATH = Path({json.dumps(str(state_path))})
target_date = sys.argv[1]
state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
state.setdefault("calls", []).append({{"kind": "bot.runner_sales_funnel_day", "date": target_date}})
state["seller_zero_dates"] = [value for value in state.get("seller_zero_dates", []) if value != target_date]
if target_date not in state.get("seller_valid_dates", []):
    state.setdefault("seller_valid_dates", []).append(target_date)
STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
""".strip()
        + "\n",
    )
    return root


def _build_fake_ai_dir(root: Path, *, state_path: Path) -> Path:
    python_path = Path(sys.executable).resolve()
    (root / "venv" / "bin").mkdir(parents=True, exist_ok=True)
    (root / "venv" / "bin" / "python").symlink_to(python_path)
    _write_script(
        root / "run_web_source_handoff.py",
        f"""
import json
import sys
from pathlib import Path

STATE_PATH = Path({json.dumps(str(state_path))})
args = sys.argv[1:]
dataset = args[args.index("--only") + 1]
date_flag = "--sales-funnel-date" if dataset == "sales-funnel" else "--search-analytics-date-to"
target_date = args[args.index(date_flag) + 1]
state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
state.setdefault("calls", []).append({{"kind": "run_web_source_handoff.py", "dataset": dataset, "date": target_date}})
STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
""".strip()
        + "\n",
    )
    return root


def _write_script(path: Path, contents: str) -> None:
    path.write_text(contents, encoding="utf-8")


def _write_state(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _read_state(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _get_json(url: str) -> dict[str, object]:
    from urllib import request as urllib_request

    with urllib_request.urlopen(url) as response:
        return json.loads(response.read().decode("utf-8"))


class _MockSellerosApi:
    def __init__(self, *, state_path: Path, requested_nm_ids: list[int]) -> None:
        self.state_path = state_path
        self.requested_nm_ids = requested_nm_ids
        self._server = HTTPServer(("127.0.0.1", _reserve_free_port()), self._build_handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def __enter__(self) -> "_MockSellerosApi":
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._server.shutdown()
        self._thread.join(timeout=5)
        self._server.server_close()

    def _build_handler(self) -> type[BaseHTTPRequestHandler]:
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                parsed = urllib_parse.urlparse(self.path)
                query = urllib_parse.parse_qs(parsed.query)
                if parsed.path == "/v1/search-analytics/snapshot":
                    self._write_search(_single_value(query, "date_to"))
                    return
                if parsed.path == "/v1/sales-funnel/daily":
                    self._write_seller(_single_value(query, "date"))
                    return
                self.send_error(404)

            def log_message(self, format: str, *args: object) -> None:  # noqa: A003
                return

            def _write_search(self, snapshot_date: str | None) -> None:
                state = _read_state(parent.state_path)
                if not snapshot_date:
                    self._write_json(404, {"detail": "search analytics not found"})
                    return
                if snapshot_date in state.get("search_valid_dates", []):
                    self._write_json(200, _build_search_payload(snapshot_date, parent.requested_nm_ids, zero=False))
                    return
                if snapshot_date in state.get("search_zero_dates", []):
                    self._write_json(200, _build_search_payload(snapshot_date, parent.requested_nm_ids, zero=True))
                    return
                if snapshot_date not in state.get("search_valid_dates", []):
                    self._write_json(404, {"detail": "search analytics not found"})
                    return
                self._write_json(200, _build_search_payload(snapshot_date, parent.requested_nm_ids, zero=False))

            def _write_seller(self, snapshot_date: str | None) -> None:
                state = _read_state(parent.state_path)
                if not snapshot_date:
                    self._write_json(404, {"detail": "snapshot_date is required"})
                    return
                if snapshot_date in state.get("seller_valid_dates", []):
                    self._write_json(200, _build_seller_payload(snapshot_date, parent.requested_nm_ids, zero=False))
                    return
                if snapshot_date in state.get("seller_zero_dates", []):
                    self._write_json(200, _build_seller_payload(snapshot_date, parent.requested_nm_ids, zero=True))
                    return
                self._write_json(404, {"detail": "seller funnel not found"})

            def _write_json(self, status: int, payload: dict[str, object]) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler


def _build_search_payload(snapshot_date: str, requested_nm_ids: list[int], *, zero: bool) -> dict[str, object]:
    return {
        "date_from": snapshot_date,
        "date_to": snapshot_date,
        "count": len(requested_nm_ids),
        "items": [
            {
                "nm_id": nm_id,
                "views_current": 0 if zero else 100 + index,
                "ctr_current": 0 if zero else 20 + index,
                "orders_current": 0 if zero else 5 + index,
                "position_avg": 0 if zero else 1 + index,
            }
            for index, nm_id in enumerate(requested_nm_ids)
        ],
    }


def _build_seller_payload(
    snapshot_date: str,
    requested_nm_ids: list[int],
    *,
    zero: bool,
) -> dict[str, object]:
    return {
        "date": snapshot_date,
        "count": len(requested_nm_ids),
        "items": [
            {
                "nm_id": nm_id,
                "name": f"NM {nm_id}",
                "vendor_code": f"VC-{nm_id}",
                "view_count": 0 if zero else 500 + index,
                "open_card_count": 0 if zero else 50 + index,
                "ctr": 0 if zero else 10 + index,
            }
            for index, nm_id in enumerate(requested_nm_ids)
        ],
    }


def _single_value(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key) or []
    if not values:
        return None
    value = str(values[0] or "").strip()
    return value or None


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


if __name__ == "__main__":
    main()
