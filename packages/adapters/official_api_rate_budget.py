"""Crash-safe cross-process request pacing for one official API family."""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import re
import threading
import time
from typing import Any, Mapping


RATE_BUDGET_CONTRACT = "wb_core_official_api_rate_budget_v1"
_SAFE_FAMILY = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")


class OfficialApiRateBudgetError(RuntimeError):
    """The durable pacing state cannot be trusted."""


class FileBackedOfficialApiRateBudget:
    """Reserve request slots under ``flock`` without persisting credentials.

    WB documents the FBS family at 300 requests/minute with a 200 ms interval.
    The caller supplies a slightly more conservative interval.  Each process
    reserves its slot before sleeping, so a crash can waste a slot but cannot
    create a request burst.
    """

    def __init__(
        self,
        *,
        runtime_dir: Path,
        family: str,
        min_interval_seconds: float,
        time_fn: Any | None = None,
        sleep_fn: Any | None = None,
    ) -> None:
        normalized_family = str(family or "").strip().casefold()
        if not _SAFE_FAMILY.fullmatch(normalized_family):
            raise ValueError("official API rate-budget family is invalid")
        interval = float(min_interval_seconds)
        if interval <= 0 or interval > 60:
            raise ValueError("official API rate-budget interval must be between 0 and 60 seconds")
        self.family = normalized_family
        self.min_interval_seconds = interval
        self.root = Path(runtime_dir).resolve() / "official_api_rate_budgets"
        self.lock_path = self.root / f"{self.family}.lock"
        self.state_path = self.root / f"{self.family}.json"
        self._time = time_fn or time.time
        self._sleep = sleep_fn or time.sleep

    def acquire(self, *, units: int = 1) -> dict[str, Any]:
        normalized_units = int(units)
        if normalized_units < 1 or normalized_units > 20:
            raise ValueError("official API rate-budget units must be between 1 and 20")
        now = float(self._time())
        with self._locked_state() as state:
            next_allowed = max(float(state.get("next_allowed_epoch") or 0.0), now)
            reserved_at = next_allowed
            state.update(
                {
                    "contract_name": RATE_BUDGET_CONTRACT,
                    "family": self.family,
                    "min_interval_seconds": self.min_interval_seconds,
                    "next_allowed_epoch": reserved_at
                    + self.min_interval_seconds * normalized_units,
                    "reservation_sequence": int(state.get("reservation_sequence") or 0) + 1,
                    "last_reserved_epoch": reserved_at,
                    "last_units": normalized_units,
                }
            )
            self._write_state(state)
        wait_seconds = max(0.0, reserved_at - now)
        if wait_seconds > 0:
            self._sleep(wait_seconds)
        return {
            "family": self.family,
            "units": normalized_units,
            "wait_seconds": wait_seconds,
            "reserved_at_epoch": reserved_at,
            "min_interval_seconds": self.min_interval_seconds,
        }

    def defer(self, seconds: float) -> None:
        delay = max(0.0, min(float(seconds), 15 * 60.0))
        now = float(self._time())
        with self._locked_state() as state:
            state.update(
                {
                    "contract_name": RATE_BUDGET_CONTRACT,
                    "family": self.family,
                    "min_interval_seconds": self.min_interval_seconds,
                    "next_allowed_epoch": max(
                        float(state.get("next_allowed_epoch") or 0.0), now + delay
                    ),
                    "last_deferred_epoch": now,
                    "last_deferred_seconds": delay,
                }
            )
            self._write_state(state)

    def snapshot(self) -> dict[str, Any]:
        with self._locked_state() as state:
            return {
                "contract_name": RATE_BUDGET_CONTRACT,
                "family": self.family,
                "min_interval_seconds": self.min_interval_seconds,
                "next_allowed_epoch": float(state.get("next_allowed_epoch") or 0.0),
                "reservation_sequence": int(state.get("reservation_sequence") or 0),
                "last_deferred_seconds": float(state.get("last_deferred_seconds") or 0.0),
            }

    class _LockedState:
        def __init__(self, owner: "FileBackedOfficialApiRateBudget") -> None:
            self.owner = owner
            self.handle: Any | None = None
            self.state: dict[str, Any] = {}

        def __enter__(self) -> dict[str, Any]:
            self.owner.root.mkdir(parents=True, exist_ok=True)
            self.handle = self.owner.lock_path.open("a+b")
            os.chmod(self.owner.lock_path, 0o600)
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
            self.state = self.owner._read_state()
            return self.state

        def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
            assert self.handle is not None
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()

    def _locked_state(self) -> "FileBackedOfficialApiRateBudget._LockedState":
        return self._LockedState(self)

    def _read_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {}
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise OfficialApiRateBudgetError(
                f"official API rate-budget state is unreadable for {self.family}"
            ) from exc
        if not isinstance(payload, Mapping):
            raise OfficialApiRateBudgetError(
                f"official API rate-budget state is invalid for {self.family}"
            )
        if payload and (
            payload.get("contract_name") != RATE_BUDGET_CONTRACT
            or payload.get("family") != self.family
        ):
            raise OfficialApiRateBudgetError(
                f"official API rate-budget identity drifted for {self.family}"
            )
        return dict(payload)

    def _write_state(self, state: Mapping[str, Any]) -> None:
        temporary = self.state_path.with_name(
            f".{self.state_path.name}.tmp.{os.getpid()}.{threading.get_ident()}"
        )
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                descriptor = -1
                json.dump(state, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.state_path)
            os.chmod(self.state_path, 0o600)
            directory_descriptor = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary.exists():
                temporary.unlink()
