"""Targeted smoke-check for promo XLSX collector contract helpers."""

from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.promo_xlsx_collector_block import (  # noqa: E402
    AUTO_PROMO_MODAL_CLOSE_SELECTOR,
    COOKIE_ACCEPT_TEXT,
    DRAWER_CLOSE_SELECTOR,
    DRAWER_OVERLAY_SELECTOR,
    TIMELINE_ACTION_SELECTOR,
    is_hydrated_state,
)
from packages.application.promo_xlsx_collector_block import (  # noqa: E402
    PromoXlsxCollectorBlock,
    build_metadata,
    classify_export_kind,
    extract_card_data,
    parse_period_text,
)
from packages.contracts.promo_xlsx_collector_block import (  # noqa: E402
    CollectorStateSnapshot,
    PromoXlsxCollectorRequest,
    PromoCardData,
)

ARTIFACTS = ROOT / "artifacts" / "promo_xlsx_collector_block" / "fixture"


def main() -> None:
    exclude_fixture = json.loads((ARTIFACTS / "workbook_headers__exclude_list_template__fixture.json").read_text(encoding="utf-8"))
    eligible_fixture = json.loads((ARTIFACTS / "workbook_headers__eligible_items_report__fixture.json").read_text(encoding="utf-8"))
    cross_year_fixture = json.loads((ARTIFACTS / "card__cross_year__fixture.json").read_text(encoding="utf-8"))

    if classify_export_kind(exclude_fixture["filename"], exclude_fixture["headers"]) != "exclude_list_template":
        raise AssertionError("exclude_list_template classification must follow filename/gating headers")
    if classify_export_kind(eligible_fixture["filename"], eligible_fixture["headers"]) != "eligible_items_report":
        raise AssertionError("eligible_items_report classification must follow filename/common headers")

    start_at, end_at, confidence = parse_period_text(cross_year_fixture["promo_period_text"], reference_year=2026)
    if start_at is not None or end_at is not None or confidence != "low":
        raise AssertionError("cross-year short labels must keep exact dates null on low confidence")

    hydrated = is_hydrated_state(
        CollectorStateSnapshot(
            ts="2026-04-20T00:00:00+05:00",
            label="fixture",
            url="https://seller.wildberries.ru/dp-promo-calendar",
            title="Акции WB",
            timeline_count=35,
            overlay_count=0,
            has_modal_close=False,
            modal_entry_count=0,
            has_configure=False,
            has_generate=False,
            has_download=False,
            has_ready=False,
            has_cookie_accept=False,
            body_excerpt="fixture",
            visible_tabs=["Доступные"],
            screenshot="/tmp/fixture.png",
        )
    )
    if not hydrated:
        raise AssertionError("hydrated state helper must accept title + timeline_count")

    if TIMELINE_ACTION_SELECTOR != '[data-testid="timeline-action"]':
        raise AssertionError("timeline selector drifted")
    if COOKIE_ACCEPT_TEXT != "Принимаю":
        raise AssertionError("cookie text drifted")
    if AUTO_PROMO_MODAL_CLOSE_SELECTOR != '[data-testid="components/auto-promo-modal/close-button-button-interface"]':
        raise AssertionError("modal close selector drifted")
    if DRAWER_CLOSE_SELECTOR != '#Portal-drawer [data-testid="pages/main-page/promo-action-wizard/drawer-close-button-button-ghost"]':
        raise AssertionError("drawer close selector drifted")
    if DRAWER_OVERLAY_SELECTOR != '#Portal-drawer [data-testid="pages/main-page/promo-action-wizard/drawer-drawer-overlay"]':
        raise AssertionError("drawer overlay selector drifted")

    ended_card = extract_card_data(
        snapshot=CollectorStateSnapshot(
            ts="2026-04-26T00:00:00+05:00",
            label="ended_fixture",
            url="https://seller.wildberries.ru/dp-promo-calendar?action=2307",
            title="Акции WB",
            timeline_count=35,
            overlay_count=1,
            has_modal_close=False,
            modal_entry_count=0,
            has_configure=False,
            has_generate=False,
            has_download=False,
            has_ready=False,
            has_cookie_accept=False,
            body_excerpt=(
                "Весенняя распродажа\n"
                "Хиты\n"
                "Автоматические скидки\n"
                "19 апреля 02:00 → 26 апреля 01:59\n"
                "Акция завершилась\n"
            ),
            visible_tabs=["Доступные"],
            screenshot="/tmp/ended-fixture.png",
        ),
        fallback_title="Весенняя распродажа Хиты Автоматические скидки",
        source_tab="Доступные",
        source_filter_code="AVAILABLE",
        reference_year=2026,
    )
    if ended_card.ui_status != "ended" or ended_card.ui_status_confidence != "high":
        raise AssertionError(f"ended UI status must be high confidence, got {ended_card}")
    if ended_card.download_action_state != "absent":
        raise AssertionError(f"ended no-download card must record absent action, got {ended_card}")
    if ended_card.campaign_identity_match is not True:
        raise AssertionError(f"ended card must keep title-match guard, got {ended_card}")

    metadata = build_metadata(
        card=PromoCardData(
            calendar_url="https://seller.wildberries.ru/dp-promo-calendar?action=2331",
            promo_id=2331,
            promo_title="Весенняя распродажа: растим заказы (автоматические скидки)",
            promo_period_text="25 апреля 02:00 → 01 мая 01:59",
            promo_start_at="2026-04-25T02:00",
            promo_end_at="2026-05-01T01:59",
            period_parse_confidence="high",
            temporal_classification="future",
            temporal_confidence="high",
            promo_status="Запланирована",
            promo_status_text="Автоакция: участие запланировано",
            eligible_count=33,
            participating_count=33,
            excluded_count=0,
            raw_card_excerpt="fixture",
            state_snapshot=CollectorStateSnapshot(
                ts="2026-04-20T00:00:00+05:00",
                label="fixture",
                url="https://seller.wildberries.ru/dp-promo-calendar?action=2331",
                title="Акции WB",
                timeline_count=35,
                overlay_count=0,
                has_modal_close=False,
                modal_entry_count=0,
                has_configure=True,
                has_generate=False,
                has_download=False,
                has_ready=False,
                has_cookie_accept=False,
                body_excerpt="fixture",
                visible_tabs=["Доступные"],
                screenshot="/tmp/fixture.png",
            ),
        ),
        trace_run_dir="/tmp/wb-core-promo-xlsx-collector-fixture",
        source_tab="Доступные",
        source_filter_code="AVAILABLE",
        export_kind="exclude_list_template",
        download=None,
        workbook=None,
    )
    metadata_payload = asdict(metadata)
    expected_keys = set(json.loads((ARTIFACTS / "metadata__canonical__fixture.json").read_text(encoding="utf-8")).keys())
    if set(metadata_payload.keys()) != expected_keys:
        raise AssertionError("metadata sidecar keys must stay stable")
    json.dumps(metadata_payload, ensure_ascii=False)

    result = PromoXlsxCollectorBlock(_HydrationExceptionDriver()).execute(
        PromoXlsxCollectorRequest(
            output_root="/tmp/wb-core-promo-hydration-exception-smoke",
            storage_state_path="/tmp/unused-storage-state.json",
            hydration_attempt_budget=1,
        )
    )
    if result.status != "blocked":
        raise AssertionError(f"hydration exception must produce blocked summary, got {result.status}")
    if len(result.hydration_attempts) != 1:
        raise AssertionError("hydration exception must persist a failed attempt summary")
    blocker = result.hydration_attempts[0].blocker or ""
    if "hydration_exception=synthetic goto timeout" not in blocker:
        raise AssertionError(f"unexpected hydration exception blocker: {blocker}")

    print("sidecar_contract: ok")
    print("export_kind_classification: ok")
    print("cross_year_parse_rule: ok")
    print("entry_reset_constants: ok")
    print("ended_status_metadata: ok")
    print("hydration_exception_surface: ok")
    print("smoke-check passed")


class _HydrationExceptionDriver:
    def start(self, request: PromoXlsxCollectorRequest) -> None:
        return

    def stop(self) -> None:
        return

    def attempt_hydration(self, attempt_num: int, label_prefix: str = "initial"):
        raise TimeoutError("synthetic goto timeout")


if __name__ == "__main__":
    main()
