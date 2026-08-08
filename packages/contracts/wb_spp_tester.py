"""Contracts and constants for the manual WB SPP tester."""

from __future__ import annotations

from typing import Literal


SPP_TEST_CONTRACT_PREFIX = "sheet_vitrina_v1_prices_spp_test"
SPP_TEST_PRICE_COUNT_MIN = 1
SPP_TEST_PRICE_COUNT_MAX = 6
SPP_TEST_HISTORY_DEFAULT_LIMIT = 20
SPP_TEST_HISTORY_MAX_LIMIT = 50
SPP_TEST_LOG_LIMIT = 10

SPP_TEST_ACTIVE_STATUSES = {
    "preflight",
    "measuring",
    "cooldown",
    "restoring",
    "running",
}
SPP_TEST_FINAL_STATUSES = {
    "complete",
    "interrupted_restored",
    "inconclusive",
    "manual_restore_required",
    "failed",
}

SppTestJobStatus = Literal[
    "preflight",
    "measuring",
    "cooldown",
    "restoring",
    "complete",
    "interrupted_restored",
    "manual_restore_required",
    "failed",
]

SppTestResultStatus = Literal[
    "success",
    "inconclusive",
    "manual_restore_required",
]
