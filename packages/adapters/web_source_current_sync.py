"""Bounded on-demand current-day sync for web-source snapshots."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
from typing import Callable
from urllib import error, parse, request as urllib_request
from zoneinfo import ZoneInfo


DEFAULT_WB_WEB_BOT_DIR = Path("/opt/wb-web-bot")
DEFAULT_WB_AI_DIR = Path("/opt/wb-ai")
DEFAULT_API_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_TIMEOUT_SEC = 600
DEFAULT_SYNC_MODE = "auto"
BUSINESS_TIMEZONE = ZoneInfo("Asia/Yekaterinburg")


@dataclass(frozen=True)
class WebSourceCurrentSyncConfig:
    mode: str
    wb_web_bot_dir: Path
    wb_ai_dir: Path
    api_base_url: str
    timeout_sec: int


@dataclass(frozen=True)
class ClosedDaySourceState:
    source_key: str
    snapshot_date: str
    row_count: int
    fetched_at: str | None


def load_web_source_current_sync_config() -> WebSourceCurrentSyncConfig:
    raw_timeout = str(
        os.environ.get("SHEET_VITRINA_WEBSOURCE_CURRENT_SYNC_TIMEOUT_SEC", DEFAULT_TIMEOUT_SEC)
    ).strip()
    try:
        timeout_sec = int(raw_timeout)
    except ValueError as exc:
        raise ValueError(
            "SHEET_VITRINA_WEBSOURCE_CURRENT_SYNC_TIMEOUT_SEC must be an integer"
        ) from exc
    if timeout_sec < 1:
        raise ValueError("SHEET_VITRINA_WEBSOURCE_CURRENT_SYNC_TIMEOUT_SEC must be positive")

    return WebSourceCurrentSyncConfig(
        mode=str(
            os.environ.get("SHEET_VITRINA_WEBSOURCE_CURRENT_SYNC_MODE", DEFAULT_SYNC_MODE)
        ).strip().lower()
        or DEFAULT_SYNC_MODE,
        wb_web_bot_dir=Path(
            str(
                os.environ.get(
                    "SHEET_VITRINA_WEBSOURCE_CURRENT_SYNC_WB_WEB_BOT_DIR",
                    DEFAULT_WB_WEB_BOT_DIR,
                )
            ).strip()
        ),
        wb_ai_dir=Path(
            str(
                os.environ.get(
                    "SHEET_VITRINA_WEBSOURCE_CURRENT_SYNC_WB_AI_DIR",
                    DEFAULT_WB_AI_DIR,
                )
            ).strip()
        ),
        api_base_url=str(
            os.environ.get(
                "SHEET_VITRINA_WEBSOURCE_CURRENT_SYNC_API_BASE_URL",
                DEFAULT_API_BASE_URL,
            )
        ).strip()
        or DEFAULT_API_BASE_URL,
        timeout_sec=timeout_sec,
    )


class ShellBackedWebSourceCurrentSync:
    """Materialize current-day Search Analytics and Seller Funnel before refresh."""

    def __init__(
        self,
        config: WebSourceCurrentSyncConfig | None = None,
        *,
        closed_day_source_state_loader: Callable[[str, str], ClosedDaySourceState | None] | None = None,
    ) -> None:
        self.config = config or load_web_source_current_sync_config()
        self._closed_day_source_state_loader = (
            closed_day_source_state_loader or self._load_closed_day_source_state
        )

    def ensure_snapshot(self, snapshot_date: str) -> None:
        if not self._is_enabled():
            return

        search_ready = self._has_search_analytics_snapshot(snapshot_date)
        seller_ready = self._has_sales_funnel_snapshot(snapshot_date)
        if search_ready and seller_ready:
            return

        bot_python = self.config.wb_web_bot_dir / "venv" / "bin" / "python"
        ai_python = self.config.wb_ai_dir / "venv" / "bin" / "python"
        bot_env = _build_env(self.config.wb_web_bot_dir / ".env")
        ai_env = _build_env(self.config.wb_ai_dir / ".env")

        if not search_ready:
            self._run(
                [str(bot_python), "-m", "bot.runner_day", snapshot_date],
                cwd=self.config.wb_web_bot_dir,
                env=bot_env,
                label=f"search_analytics current-day sync {snapshot_date}",
            )
            self._run(
                [
                    str(ai_python),
                    "run_web_source_handoff.py",
                    "--only",
                    "search-analytics",
                    "--search-analytics-date-to",
                    snapshot_date,
                ],
                cwd=self.config.wb_ai_dir,
                env=ai_env,
                label=f"search_analytics handoff {snapshot_date}",
            )

        if not seller_ready:
            self._run(
                [str(bot_python), "-m", "bot.runner_sales_funnel_day", snapshot_date],
                cwd=self.config.wb_web_bot_dir,
                env=bot_env,
                label=f"sales_funnel current-day sync {snapshot_date}",
            )
            self._run(
                [
                    str(ai_python),
                    "run_web_source_handoff.py",
                    "--only",
                    "sales-funnel",
                    "--sales-funnel-date",
                    snapshot_date,
                ],
                cwd=self.config.wb_ai_dir,
                env=ai_env,
                label=f"sales_funnel handoff {snapshot_date}",
            )

        missing: list[str] = []
        if not self._has_search_analytics_snapshot(snapshot_date):
            missing.append("search_analytics")
        if not self._has_sales_funnel_snapshot(snapshot_date):
            missing.append("sales_funnel")
        if missing:
            raise RuntimeError(
                "current-day web-source sync finished without exact-date materialization for "
                f"{','.join(missing)} on {snapshot_date}"
            )

    def ensure_closed_day_snapshot(self, *, source_key: str, snapshot_date: str) -> None:
        if not self._is_enabled():
            raise RuntimeError("closed-day web-source sync is disabled in current runtime")
        if source_key == "web_source_snapshot":
            self._materialize_search_analytics(snapshot_date)
            self._ensure_closed_day_source_freshness(source_key=source_key, snapshot_date=snapshot_date)
            return
        if source_key == "seller_funnel_snapshot":
            self._materialize_sales_funnel(snapshot_date)
            self._ensure_closed_day_source_freshness(source_key=source_key, snapshot_date=snapshot_date)
            return
        raise ValueError(f"unsupported closed-day web-source source_key: {source_key}")

    def _is_enabled(self) -> bool:
        if self.config.mode == "off":
            return False
        if self.config.mode == "force":
            return True
        if self.config.mode != "auto":
            raise ValueError(
                "SHEET_VITRINA_WEBSOURCE_CURRENT_SYNC_MODE must be one of: auto, force, off"
            )
        return self.config.wb_web_bot_dir.exists() and self.config.wb_ai_dir.exists()

    def _has_search_analytics_snapshot(self, snapshot_date: str) -> bool:
        payload = _fetch_json(
            f"{self.config.api_base_url.rstrip('/')}/v1/search-analytics/snapshot"
            f"?{parse.urlencode({'date_to': snapshot_date})}"
        )
        return _is_usable_search_analytics_payload(payload, snapshot_date)

    def _has_sales_funnel_snapshot(self, snapshot_date: str) -> bool:
        payload = _fetch_json(
            f"{self.config.api_base_url.rstrip('/')}/v1/sales-funnel/daily"
            f"?{parse.urlencode({'date': snapshot_date})}"
        )
        return _is_usable_sales_funnel_payload(payload, snapshot_date)

    def _run(
        self,
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        label: str,
    ) -> None:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=self.config.timeout_sec,
        )
        if completed.returncode == 0:
            return
        stdout_tail = completed.stdout.strip()[-800:]
        stderr_tail = completed.stderr.strip()[-800:]
        details = "; ".join(
            part
            for part in [
                f"stdout={stdout_tail}" if stdout_tail else "",
                f"stderr={stderr_tail}" if stderr_tail else "",
            ]
            if part
        )
        raise RuntimeError(f"{label} failed (rc={completed.returncode}){': ' + details if details else ''}")

    def _materialize_search_analytics(self, snapshot_date: str) -> None:
        bot_python = self.config.wb_web_bot_dir / "venv" / "bin" / "python"
        ai_python = self.config.wb_ai_dir / "venv" / "bin" / "python"
        bot_env = _build_env(self.config.wb_web_bot_dir / ".env")
        ai_env = _build_env(self.config.wb_ai_dir / ".env")
        self._run(
            [str(bot_python), "-m", "bot.runner_day", snapshot_date],
            cwd=self.config.wb_web_bot_dir,
            env=bot_env,
            label=f"search_analytics sync {snapshot_date}",
        )
        self._run(
            [
                str(ai_python),
                "run_web_source_handoff.py",
                "--only",
                "search-analytics",
                "--search-analytics-date-to",
                snapshot_date,
            ],
            cwd=self.config.wb_ai_dir,
            env=ai_env,
            label=f"search_analytics handoff {snapshot_date}",
        )

    def _materialize_sales_funnel(self, snapshot_date: str) -> None:
        bot_python = self.config.wb_web_bot_dir / "venv" / "bin" / "python"
        ai_python = self.config.wb_ai_dir / "venv" / "bin" / "python"
        bot_env = _build_env(self.config.wb_web_bot_dir / ".env")
        ai_env = _build_env(self.config.wb_ai_dir / ".env")
        self._run(
            [str(bot_python), "-m", "bot.runner_sales_funnel_day", snapshot_date],
            cwd=self.config.wb_web_bot_dir,
            env=bot_env,
            label=f"sales_funnel sync {snapshot_date}",
        )
        self._run(
            [
                str(ai_python),
                "run_web_source_handoff.py",
                "--only",
                "sales-funnel",
                "--sales-funnel-date",
                snapshot_date,
            ],
            cwd=self.config.wb_ai_dir,
            env=ai_env,
            label=f"sales_funnel handoff {snapshot_date}",
        )

    def _ensure_closed_day_source_freshness(self, *, source_key: str, snapshot_date: str) -> None:
        state = self._closed_day_source_state_loader(source_key, snapshot_date)
        if state is None or state.row_count <= 0:
            raise RuntimeError(
                "closed_day_source_freshness_not_accepted: "
                f"source_key={source_key}; snapshot_date={snapshot_date}; reason=source_rows_missing_after_sync"
            )
        if not state.fetched_at:
            raise RuntimeError(
                "closed_day_source_freshness_not_accepted: "
                f"source_key={source_key}; snapshot_date={snapshot_date}; reason=source_fetched_at_missing"
            )
        fetched_at = _parse_timestamp(state.fetched_at)
        required_after = _closed_day_required_fetched_after(snapshot_date)
        if fetched_at < required_after:
            raise RuntimeError(
                "closed_day_source_freshness_not_accepted: "
                f"source_key={source_key}; snapshot_date={snapshot_date}; "
                f"source_fetched_at={fetched_at.isoformat()}; required_after={required_after.isoformat()}"
            )

    def _load_closed_day_source_state(self, source_key: str, snapshot_date: str) -> ClosedDaySourceState | None:
        ai_python = self.config.wb_ai_dir / "venv" / "bin" / "python"
        ai_env = _build_env(self.config.wb_ai_dir / ".env")
        probe = subprocess.run(
            [
                str(ai_python),
                "-c",
                _CLOSED_DAY_SOURCE_STATE_PROBE_SCRIPT,
                source_key,
                snapshot_date,
            ],
            cwd=str(self.config.wb_ai_dir),
            env=ai_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=self.config.timeout_sec,
        )
        if probe.returncode != 0:
            stderr_tail = probe.stderr.strip()[-800:]
            stdout_tail = probe.stdout.strip()[-800:]
            details = "; ".join(
                part
                for part in [
                    f"stdout={stdout_tail}" if stdout_tail else "",
                    f"stderr={stderr_tail}" if stderr_tail else "",
                ]
                if part
            )
            raise RuntimeError(
                "closed_day_source_freshness_probe_failed: "
                f"source_key={source_key}; snapshot_date={snapshot_date}"
                f"{': ' + details if details else ''}"
            )
        payload = json.loads(probe.stdout or "{}")
        return ClosedDaySourceState(
            source_key=source_key,
            snapshot_date=snapshot_date,
            row_count=int(payload.get("row_count", 0) or 0),
            fetched_at=str(payload.get("fetched_at", "") or "") or None,
        )


def _build_env(env_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(_load_env_file(env_path))
    return env


def _load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _fetch_json(url: str) -> dict[str, object] | None:
    request = urllib_request.Request(url, method="GET")
    try:
        with urllib_request.urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        if exc.code == 404:
            return None
        body = exc.read().decode("utf-8")
        raise RuntimeError(f"current-day web-source probe failed with status {exc.code}: {body}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"current-day web-source probe failed: {exc}") from exc


def _is_usable_sales_funnel_payload(
    payload: dict[str, object] | None,
    snapshot_date: str,
) -> bool:
    if not isinstance(payload, dict):
        return False
    if str(payload.get("date", "") or "") != snapshot_date:
        return False
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        return False
    return any(
        _item_metric_value(item, "view_count") > 0 or _item_metric_value(item, "open_card_count") > 0
        for item in items
        if isinstance(item, dict)
    )


def _is_usable_search_analytics_payload(
    payload: dict[str, object] | None,
    snapshot_date: str,
) -> bool:
    if not isinstance(payload, dict):
        return False
    if str(payload.get("date_to", "") or "") != snapshot_date:
        return False
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        return False
    return any(
        _item_metric_value(item, "views_current") > 0
        or _item_metric_value(item, "ctr_current") > 0
        or _item_metric_value(item, "orders_current") > 0
        for item in items
        if isinstance(item, dict)
    )


def _item_metric_value(item: dict[str, object], key: str) -> float:
    value = item.get(key)
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


def _closed_day_required_fetched_after(snapshot_date: str) -> datetime:
    snapshot_day = date.fromisoformat(snapshot_date)
    next_business_day_start = datetime.combine(
        snapshot_day + timedelta(days=1),
        time(0, 0),
        tzinfo=BUSINESS_TIMEZONE,
    )
    return next_business_day_start.astimezone(timezone.utc)


def _parse_timestamp(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


_CLOSED_DAY_SOURCE_STATE_PROBE_SCRIPT = r"""
import json
import os
import sys
import psycopg2

source_key = sys.argv[1]
snapshot_date = sys.argv[2]

if source_key == "web_source_snapshot":
    conn = psycopg2.connect(
        host=os.environ["WEB_SOURCE_SRC_PGHOST"],
        port=os.environ["WEB_SOURCE_SRC_PGPORT"],
        dbname=os.environ["WEB_SOURCE_SRC_PGDATABASE"],
        user=os.environ["WEB_SOURCE_SRC_PGUSER"],
        password=os.environ["WEB_SOURCE_SRC_PGPASSWORD"],
        connect_timeout=5,
    )
    sql = (
        "select count(*), max(fetched_at) "
        "from public.search_analytics_raw "
        "where date_to = %s::date"
    )
elif source_key == "seller_funnel_snapshot":
    conn = psycopg2.connect(
        host=os.environ["PGHOST"],
        port=os.environ["PGPORT"],
        dbname=os.environ["PGDATABASE"],
        user=os.environ["PGUSER"],
        password=os.environ["PGPASSWORD"],
        connect_timeout=5,
    )
    sql = (
        "select count(*), max(source_fetched_at) "
        "from public.web_source_sales_funnel_daily "
        "where snapshot_date = %s::date"
    )
else:
    raise SystemExit(f"unsupported source_key: {source_key}")

with conn:
    with conn.cursor() as cur:
        cur.execute(sql, (snapshot_date,))
        row = cur.fetchone()

row_count = int(row[0] or 0) if row else 0
fetched_at = row[1].isoformat() if row and row[1] is not None else ""
print(json.dumps({"row_count": row_count, "fetched_at": fetched_at}))
"""
