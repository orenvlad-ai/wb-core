"""Targeted smoke-check for promo XLSX collector contract helpers."""

from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.promo_xlsx_collector_block import (  # noqa: E402
    AUTO_PROMO_MODAL_CLOSE_SELECTOR,
    COOKIE_ACCEPT_TEXT,
    DRAWER_CLOSE_SELECTOR,
    DRAWER_OVERLAY_SELECTOR,
    PROMOTIONS_TIMELINE_PATH,
    TIMELINE_ACTION_SELECTOR,
    _build_campaign_manifest_snapshot,
    is_hydrated_state,
)
from packages.application.promo_xlsx_collector_block import (  # noqa: E402
    PromoXlsxCollectorBlock,
    build_metadata,
    build_timeline_candidate,
    classify_timeline_preflight,
    classify_export_kind,
    extract_card_data,
    parse_period_text,
)
from packages.contracts.promo_xlsx_collector_block import (  # noqa: E402
    CampaignManifestItem,
    CampaignManifestSnapshot,
    CollectorStateSnapshot,
    DrawerResetSummary,
    PromoXlsxCollectorRequest,
    PromoCardData,
    TimelineBlockSnapshot,
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

    timeline_candidate = build_timeline_candidate(
        TimelineBlockSnapshot(
            index=0,
            raw_text=(
                "Весенняя распродажа Хиты Автоматические скидки\n"
                "19 апреля - 26 апреля\n"
                "Акция завершилась"
            ),
        )
    )
    if timeline_candidate is None:
        raise AssertionError("timeline ended fixture must produce a candidate")
    timeline_decision = classify_timeline_preflight(timeline_candidate)
    if timeline_decision["timeline_classification_decision"] != "timeline_non_materializable_expected":
        raise AssertionError(f"timeline ended fixture must avoid drawer, got {timeline_decision}")
    if timeline_candidate.timeline_status != "ended" or timeline_candidate.timeline_status_confidence != "high":
        raise AssertionError(f"timeline ended fixture must be high confidence, got {timeline_candidate}")

    manifest_snapshot = _build_campaign_manifest_snapshot(
        payload={
            "data": {
                "promotions": [
                    {
                        "promoID": 2307,
                        "name": "Весенняя распродажа: хиты - автоматические скидки",
                        "type": "AUTO_PROMO",
                        "startDate": "2026-04-18T21:00:00Z",
                        "endDate": "2026-04-25T20:59:59Z",
                        "participation": {
                            "status": "SKIPPED",
                            "counts": {"eligible": 33},
                        },
                    }
                ]
            }
        },
        source_path=PROMOTIONS_TIMELINE_PATH,
        started_perf=time.perf_counter(),
    )
    if not manifest_snapshot.manifest_loaded_success or manifest_snapshot.manifest_campaign_count != 1:
        raise AssertionError(f"manifest parser must load compact campaign snapshot, got {manifest_snapshot}")
    manifest_item = manifest_snapshot.campaigns[0]
    if manifest_item.lifecycle_status != "ended" or manifest_item.downloadability != "not_available":
        raise AssertionError(f"manifest parser must classify ended/non-downloadable, got {manifest_item}")
    if manifest_item.period_text != "19 - 26 апреля":
        raise AssertionError(f"manifest parser must preserve timeline-compatible period text, got {manifest_item}")

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

    preflight_result = PromoXlsxCollectorBlock(_EndedNoDownloadDriver()).execute(
        PromoXlsxCollectorRequest(
            output_root="/tmp/wb-core-promo-ended-preflight-smoke",
            storage_state_path="/tmp/unused-storage-state.json",
            hydration_attempt_budget=1,
        )
    )
    if preflight_result.status != "success":
        raise AssertionError(f"ended preflight run must finish successfully, got {preflight_result.status}")
    if preflight_result.early_ended_no_download_count != 1:
        raise AssertionError(f"expected one early ended/no-download campaign, got {preflight_result}")
    if preflight_result.drawer_open_avoided_count != 1 or preflight_result.opened_drawer_count != 0:
        raise AssertionError(f"timeline ended campaign must avoid drawer, got {preflight_result}")
    if preflight_result.timeline_non_materializable_count != 1:
        raise AssertionError(f"expected one timeline non-materializable campaign, got {preflight_result}")
    if preflight_result.deep_workbook_flow_count != 0:
        raise AssertionError(f"ended no-download must not enter deep workbook flow, got {preflight_result}")
    if preflight_result.generate_screen_attempt_count != 0 or preflight_result.download_attempt_count != 0:
        raise AssertionError(f"ended no-download must not attempt generate/download, got {preflight_result}")
    promo = preflight_result.promos[0]
    if promo.early_preflight_decision != "timeline_non_materializable_expected":
        raise AssertionError(f"expected timeline non-materializable decision, got {promo}")
    if promo.drawer_opened is not False or promo.drawer_skip_reason != "timeline_ended_non_materializable":
        raise AssertionError(f"expected drawer skip diagnostics, got {promo}")
    if promo.heavy_flow_required is not False or promo.non_materializable_reason != "ended_without_download":
        raise AssertionError(f"expected heavy flow avoided for ended/no-download, got {promo}")

    unknown_timeline_result = PromoXlsxCollectorBlock(_TimelineUnknownDrawerDriver()).execute(
        PromoXlsxCollectorRequest(
            output_root="/tmp/wb-core-promo-timeline-unknown-full-flow-smoke",
            storage_state_path="/tmp/unused-storage-state.json",
            hydration_attempt_budget=1,
        )
    )
    if unknown_timeline_result.status != "success":
        raise AssertionError(f"unknown timeline fallback run must finish successfully, got {unknown_timeline_result.status}")
    if unknown_timeline_result.drawer_open_avoided_count != 0 or unknown_timeline_result.opened_drawer_count != 1:
        raise AssertionError(f"unknown timeline evidence must open drawer, got {unknown_timeline_result}")
    unknown_promo = unknown_timeline_result.promos[0]
    if unknown_promo.timeline_classification_decision != "unknown_full_flow":
        raise AssertionError(f"unknown timeline must preserve full flow decision, got {unknown_promo}")
    if unknown_promo.drawer_opened is not True:
        raise AssertionError(f"unknown timeline fallback must record drawer_opened=true, got {unknown_promo}")

    manifest_result = PromoXlsxCollectorBlock(_ManifestEndedNoDrawerDriver()).execute(
        PromoXlsxCollectorRequest(
            output_root="/tmp/wb-core-promo-manifest-ended-no-drawer-smoke",
            storage_state_path="/tmp/unused-storage-state.json",
            hydration_attempt_budget=1,
        )
    )
    if manifest_result.status != "success":
        raise AssertionError(f"manifest shortcut run must finish successfully, got {manifest_result.status}")
    if not manifest_result.manifest_loaded_success or manifest_result.manifest_source != "network_response":
        raise AssertionError(f"manifest snapshot must be surfaced in summary, got {manifest_result}")
    if manifest_result.manifest_drawer_avoid_count != 1 or manifest_result.drawer_open_avoided_count != 1:
        raise AssertionError(f"manifest ended campaign must avoid drawer, got {manifest_result}")
    if manifest_result.opened_drawer_count != 0 or manifest_result.drawer_open_required_count != 0:
        raise AssertionError(f"manifest ended campaign must not open drawer, got {manifest_result}")
    manifest_promo = manifest_result.promos[0]
    if manifest_promo.metadata.manifest_decision != "drawer_avoid_manifest_non_materializable":
        raise AssertionError(f"manifest decision must be recorded, got {manifest_promo}")
    if manifest_promo.metadata.manifest_match_confidence != "high":
        raise AssertionError(f"manifest match must be high confidence, got {manifest_promo}")
    if manifest_promo.metadata.drawer_skip_reason != "manifest_ended_non_materializable":
        raise AssertionError(f"manifest drawer skip reason must be explicit, got {manifest_promo}")

    print("sidecar_contract: ok")
    print("export_kind_classification: ok")
    print("cross_year_parse_rule: ok")
    print("entry_reset_constants: ok")
    print("ended_status_metadata: ok")
    print("timeline_status_classifier: ok")
    print("manifest_parser: ok")
    print("ended_no_download_preflight: ok")
    print("timeline_unknown_full_flow: ok")
    print("manifest_ended_no_drawer: ok")
    print("hydration_exception_surface: ok")
    print("smoke-check passed")


class _HydrationExceptionDriver:
    def start(self, request: PromoXlsxCollectorRequest) -> None:
        return

    def stop(self) -> None:
        return

    def attempt_hydration(self, attempt_num: int, label_prefix: str = "initial"):
        raise TimeoutError("synthetic goto timeout")


class _EndedNoDownloadDriver:
    def start(self, request: PromoXlsxCollectorRequest) -> None:
        Path("/tmp/ended-fixture.png").write_bytes(b"fixture")
        return

    def stop(self) -> None:
        return

    def attempt_hydration(self, attempt_num: int, label_prefix: str = "initial"):
        from packages.contracts.promo_xlsx_collector_block import HydrationAttemptSummary

        return HydrationAttemptSummary(
            attempt_num=attempt_num,
            entry_strategy="direct_open",
            cookie_clicked=False,
            hydrated_success=True,
            title="Акции WB",
            url="https://seller.wildberries.ru/dp-promo-calendar",
            timeline_count=1,
            overlay_count=0,
            time_to_hydrated_sec=0.1,
        )

    def enumerate_timeline_blocks(self, max_candidates):
        return [
            TimelineBlockSnapshot(
                index=0,
                raw_text=(
                    "Весенняя распродажа Хиты Автоматические скидки\n"
                    "19 апреля - 26 апреля\n"
                    "Акция завершилась"
                ),
            )
        ]

    def open_timeline_candidate(self, candidate):
        raise AssertionError("timeline ended/no-download classifier must not open drawer")

    def open_generate_screen(self, slug: str):
        raise AssertionError("ended/no-download preflight must not open generate screen")

    def generate_file_and_wait_ready(self, slug: str):
        raise AssertionError("ended/no-download preflight must not generate workbook")

    def download_current_workbook(self):
        raise AssertionError("ended/no-download preflight must not download workbook")

    def reset_drawer(self, label: str) -> DrawerResetSummary:
        return DrawerResetSummary(
            clicked=True,
            selector="#Portal-drawer close",
            overlay_before=1,
            success=True,
            after_state_path=None,
        )

    def current_timeline_count(self) -> int:
        return 1

    def current_url(self) -> str:
        return "https://seller.wildberries.ru/dp-promo-calendar?action=2307"

    def last_state_snapshot(self):
        return None


class _TimelineUnknownDrawerDriver(_EndedNoDownloadDriver):
    def enumerate_timeline_blocks(self, max_candidates):
        return [
            TimelineBlockSnapshot(
                index=0,
                raw_text="Весенняя распродажа Хиты Автоматические скидки\n19 апреля - 26 апреля",
            )
        ]

    def open_timeline_candidate(self, candidate):
        return CollectorStateSnapshot(
            ts="2026-04-26T00:00:00+05:00",
            label="ended_fixture",
            url="https://seller.wildberries.ru/dp-promo-calendar?action=2307",
            title="Акции WB",
            timeline_count=1,
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
        )


class _ManifestEndedNoDrawerDriver(_EndedNoDownloadDriver):
    def enumerate_timeline_blocks(self, max_candidates):
        return [
            TimelineBlockSnapshot(
                index=0,
                raw_text=(
                    "Весенняя распродажа: хиты - автоматические скидки\n"
                    "19 - 26 апреля"
                ),
            )
        ]

    def campaign_manifest_snapshot(self):
        return CampaignManifestSnapshot(
            manifest_source="network_response",
            manifest_loaded_success=True,
            manifest_campaign_count=1,
            manifest_loaded_at="2026-04-26T23:49:48+05:00",
            manifest_source_path="/ns/calendar-api/dp-calendar/web/api/v3/promotions/timeline",
            campaigns=[
                CampaignManifestItem(
                    campaign_id="2307",
                    promo_id=2307,
                    title="Весенняя распродажа: хиты - автоматические скидки",
                    period_text="19 - 26 апреля",
                    start_at="2026-04-19T02:00",
                    end_at="2026-04-26T01:59",
                    lifecycle_status="ended",
                    lifecycle_status_confidence="high",
                    participation_status="SKIPPED",
                    downloadability="not_available",
                    downloadability_confidence="high",
                    goods_count=33,
                    autoaction_marker="auto_promo",
                    raw_status_code="SKIPPED",
                    confidence="high",
                    evidence_sources=[
                        "manifest_end_date_elapsed",
                        "manifest_participation_status",
                        "manifest_period",
                        "manifest_promo_id",
                    ],
                    loaded_at="2026-04-26T23:49:48+05:00",
                )
            ],
        )

    def open_timeline_candidate(self, candidate):
        raise AssertionError("manifest high-confidence ended campaign must not open drawer")


if __name__ == "__main__":
    main()
