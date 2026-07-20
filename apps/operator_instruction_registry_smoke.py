"""Deterministic content/update-registry guards for operator instructions."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    _render_instruction_new_badge,
    _render_operator_instruction_block,
    _render_operator_instruction_update_item,
    _render_sheet_vitrina_instructions_ui,
)
from packages.application.operator_instruction_content.supply_management import (  # noqa: E402
    WB_WAREHOUSE_SELECTION_CALCULATE_LABEL,
    WB_WAREHOUSE_SELECTION_EXCLUDE_LABEL,
    WB_WAREHOUSE_SELECTION_PLAN_LABEL,
    WB_WAREHOUSE_SELECTION_RECOMMENDATION_LABEL,
    WB_WAREHOUSE_SELECTION_ROUTE,
)
from packages.application.operator_instructions import (  # noqa: E402
    INSTRUCTION_NEW_WINDOW_DAYS,
    InstructionBlock,
    PUBLISHED_OPERATOR_INSTRUCTION_UPDATES,
    build_instruction_new_state,
    get_operator_instruction,
    is_instruction_update_new,
    list_operator_instruction_updates,
    list_operator_instructions,
    validate_operator_instruction_registry,
    validate_operator_instruction_updates,
)
from packages.contracts.wb_supply_planning_zones import CENTRAL_STORAGE_WAREHOUSES  # noqa: E402


EXPECTED_SECTION_ORDER = (
    "role",
    "find-shipment",
    "shipment-dates",
    "documents",
    "not-manager-work",
    "wb-warehouse-selection",
    "fulfillment-services",
    "final-check",
    "escalation",
)


def main() -> None:
    validate_operator_instruction_registry()
    validate_operator_instruction_updates()
    instructions = list_operator_instructions()
    _assert(len(instructions) == 1, "published instruction registry stays compatible")
    instruction = instructions[0]
    _assert(get_operator_instruction() is instruction, "empty id resolves the first instruction")
    _assert(get_operator_instruction("supply-management") is instruction, "published id resolves")
    _assert(get_operator_instruction("missing") is None, "unknown id stays controlled")
    _assert(instruction.revision == 3, "supply-management revision must be incremented")
    _assert(tuple(section.anchor for section in instruction.sections) == EXPECTED_SECTION_ORDER, "existing sections stay ordered and the new section precedes fulfillment services")

    _assert_registry_uniqueness(instructions)
    _assert_validation_failures(instruction)
    _assert_update_and_new_semantics(instruction)
    _assert_warehouse_instruction_contract(instruction)
    _assert_operator_ui_label_contract(instruction)
    _assert_safe_rendering(instruction)
    print("operator_instruction_registry_smoke: OK")


def _assert_registry_uniqueness(instructions) -> None:
    instruction_ids = [item.instruction_id for item in instructions]
    _assert(len(instruction_ids) == len(set(instruction_ids)), "instruction ids must be unique")
    all_block_ids: list[str] = []
    for instruction in instructions:
        anchors = [section.anchor for section in instruction.sections]
        _assert(len(anchors) == len(set(anchors)), "section anchors must be unique inside an instruction")
        for section in instruction.sections:
            all_block_ids.extend(block.block_id for block in section.blocks)
    _assert(len(all_block_ids) == len(set(all_block_ids)), "block ids must be globally unique")
    _assert(all(re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value) for value in all_block_ids), "block ids must be stable slugs")


def _assert_validation_failures(instruction) -> None:
    duplicate_section = replace(
        instruction,
        sections=instruction.sections + (instruction.sections[0],),
    )
    _expect_value_error(
        lambda: validate_operator_instruction_registry((duplicate_section,)),
        "duplicate section anchor must fail",
    )
    first_section = instruction.sections[0]
    duplicate_block_section = replace(
        first_section,
        blocks=first_section.blocks + (first_section.blocks[0],),
    )
    duplicate_block_instruction = replace(
        instruction,
        sections=(duplicate_block_section, *instruction.sections[1:]),
    )
    _expect_value_error(
        lambda: validate_operator_instruction_registry((duplicate_block_instruction,)),
        "duplicate block id must fail",
    )
    _expect_value_error(
        lambda: validate_operator_instruction_registry((instruction, instruction)),
        "duplicate instruction id must fail",
    )

    update = PUBLISHED_OPERATOR_INSTRUCTION_UPDATES[0]
    _expect_value_error(
        lambda: validate_operator_instruction_updates(
            (instruction,),
            (replace(update, instruction_id="missing-instruction"),),
        ),
        "unknown instruction update reference must fail",
    )
    _expect_value_error(
        lambda: validate_operator_instruction_updates(
            (instruction,),
            (
                replace(
                    update,
                    section_anchors=("missing-section",),
                    block_ids=(),
                    target_id="missing-section",
                    new_section_anchors=("missing-section",),
                ),
            ),
        ),
        "unknown section update reference must fail",
    )
    _expect_value_error(
        lambda: validate_operator_instruction_updates(
            (instruction,),
            (
                replace(
                    update,
                    block_ids=("missing-block",),
                    target_id="missing-block",
                    new_section_anchors=(),
                    new_block_ids=("missing-block",),
                ),
            ),
        ),
        "unknown block update reference must fail",
    )
    _expect_value_error(
        lambda: validate_operator_instruction_updates(
            (instruction,),
            (replace(update, instruction_revision=instruction.revision + 1),),
        ),
        "future update revision must fail",
    )
    _expect_value_error(
        lambda: validate_operator_instruction_updates(
            (instruction,),
            (replace(update, published_on="2026-07-20"),),
        ),
        "non-date publication value must fail",
    )
    _expect_value_error(
        lambda: validate_operator_instruction_updates(
            (instruction,),
            (replace(update, block_ids=update.block_ids + (update.block_ids[0],)),),
        ),
        "duplicate update block reference must fail",
    )


def _assert_update_and_new_semantics(instruction) -> None:
    updates = list_operator_instruction_updates()
    _assert(updates == tuple(sorted(updates, key=lambda item: (item.published_on, item.update_id), reverse=True)), "updates render newest first")
    _assert(
        tuple(update.update_id for update in PUBLISHED_OPERATOR_INSTRUCTION_UPDATES)
        == (
            "supply-management-r2-wb-warehouse-selection",
            "supply-management-r3-exact-wb-supply-composition",
        ),
        "update registry must retain both append-only sequential entries",
    )
    update = updates[0]
    previous_update = updates[1]
    _assert(update.update_id == "supply-management-r3-exact-wb-supply-composition", "revision 3 update must be latest")
    _assert(update.instruction_revision == instruction.revision == 3, "latest update revision matches instruction revision")
    _assert(update.source_type == "owner_audio_instruction", "normalized update source type is retained")
    _assert(update.section_anchors == ("wb-warehouse-selection",), "revision 3 update references the existing section")
    _assert(update.block_ids == ("wb-warehouse-selection-exact-composition",), "revision 3 update references the exact block")
    _assert(update.new_section_anchors == (), "revision 3 does not recreate the existing section")
    _assert(update.new_block_ids == ("wb-warehouse-selection-exact-composition",), "revision 3 marks the exact block as new")
    _assert(update.target_id == "wb-warehouse-selection-exact-composition", "update target is the exact block DOM id")
    before = update.published_on - timedelta(days=1)
    last_active = update.published_on + timedelta(days=INSTRUCTION_NEW_WINDOW_DAYS - 1)
    expired = update.published_on + timedelta(days=INSTRUCTION_NEW_WINDOW_DAYS)
    _assert(not is_instruction_update_new(update, before), "future NEW must be inactive")
    _assert(is_instruction_update_new(update, update.published_on), "NEW must start on publication date")
    _assert(is_instruction_update_new(update, last_active), "NEW must remain active through day 29")
    _assert(not is_instruction_update_new(update, expired), "NEW must expire on day 30")

    whole_section_state = build_instruction_new_state(
        instruction,
        previous_update.published_on,
        (previous_update,),
    )
    _assert(whole_section_state.instruction_is_new, "a new section makes the instruction new")
    _assert(whole_section_state.new_section_anchors == frozenset({"wb-warehouse-selection"}), "new section badge is active")
    _assert(whole_section_state.topic_section_anchors == frozenset({"wb-warehouse-selection"}), "new section marks its topic")
    _assert(not whole_section_state.new_block_ids, "one update creating a whole section suppresses child block badges")
    warehouse_section = next(section for section in instruction.sections if section.anchor == "wb-warehouse-selection")
    whole_section_blocks_html = "".join(
        _render_operator_instruction_block(
            block,
            is_new=block.block_id in whole_section_state.new_block_ids,
        )
        for block in warehouse_section.blocks
    )
    _assert('class="new-badge"' not in whole_section_blocks_html, "whole-section update renders no duplicate child NEW")

    active_state = build_instruction_new_state(instruction, update.published_on)
    _assert(active_state.instruction_is_new, "active section and later block make the instruction new")
    _assert(active_state.new_section_anchors == frozenset({"wb-warehouse-selection"}), "older section badge remains active")
    _assert(
        active_state.new_block_ids == frozenset({"wb-warehouse-selection-exact-composition"}),
        "a later block keeps its own badge while the parent section is still new",
    )
    _assert(active_state.topic_section_anchors == frozenset({"wb-warehouse-selection"}), "later block keeps the parent topic new")
    _assert(not build_instruction_new_state(instruction, before).instruction_is_new, "future state is inactive")
    _assert(not build_instruction_new_state(instruction, expired).instruction_is_new, "expired state is inactive")

    block_update = replace(
        update,
        update_id="supply-management-r3-documents-block-example",
        section_anchors=("documents",),
        block_ids=("documents-conflict",),
        target_id="documents-conflict",
        new_section_anchors=(),
        new_block_ids=("documents-conflict",),
    )
    block_state = build_instruction_new_state(
        instruction,
        update.published_on,
        (block_update,),
    )
    _assert(block_state.new_section_anchors == frozenset(), "block-only update does not mark the old section heading")
    _assert(block_state.new_block_ids == frozenset({"documents-conflict"}), "new block receives its own badge")
    _assert(block_state.topic_section_anchors == frozenset({"documents"}), "new block marks its parent topic navigation")

    active_html = _render_sheet_vitrina_instructions_ui(instruction, business_date=update.published_on)
    expired_html = _render_sheet_vitrina_instructions_ui(instruction, business_date=expired)
    _assert('<details class="instruction-updates" open>' in active_html, "updates disclose automatically while NEW is active")
    _assert('<details class="instruction-updates">' in expired_html, "update history remains after NEW expires")
    _assert('href="/sheet-vitrina-v1/instructions?embedded=1&amp;instruction=supply-management#wb-warehouse-selection-exact-composition"' in active_html, "latest update links to exact block DOM id")
    _assert('href="/sheet-vitrina-v1/instructions?embedded=1&amp;instruction=supply-management#wb-warehouse-selection"' in active_html, "previous update keeps its exact section link")
    new_section_html = active_html.split('<section class="instruction-section" id="wb-warehouse-selection">', 1)[1].split('</section>', 1)[0]
    _assert(new_section_html.count('class="new-badge"') == 2, "still-new section has its heading badge plus the later block badge")
    new_block_html = new_section_html.split('id="wb-warehouse-selection-exact-composition"', 1)[1].split('</aside>', 1)[0]
    _assert(new_block_html.count('class="new-badge"') == 1, "revision 3 block renders one direct NEW badge")
    expired_section_html = expired_html.split('<section class="instruction-section" id="wb-warehouse-selection">', 1)[1].split('</section>', 1)[0]
    _assert('class="new-badge"' not in expired_section_html, "expired section badge disappears deterministically")


def _assert_warehouse_instruction_contract(instruction) -> None:
    section = next(section for section in instruction.sections if section.anchor == "wb-warehouse-selection")
    exact_composition_blocks = [
        block
        for block in section.blocks
        if block.block_id == "wb-warehouse-selection-exact-composition"
    ]
    _assert(len(exact_composition_blocks) == 1, "exact composition block must be unique in the warehouse section")
    _assert(exact_composition_blocks[0].kind == "important", "exact composition rule must use a visible callout")
    text = _section_text(section)
    for required in (
        WB_WAREHOUSE_SELECTION_ROUTE,
        WB_WAREHOUSE_SELECTION_EXCLUDE_LABEL,
        WB_WAREHOUSE_SELECTION_CALCULATE_LABEL,
        WB_WAREHOUSE_SELECTION_PLAN_LABEL,
        WB_WAREHOUSE_SELECTION_RECOMMENDATION_LABEL,
        "#1",
        "ЦФО Север",
        "ЦФО Восток",
        "ЦФО Юг",
        "Excel-распределение",
        "фактическое состояние кабинета WB",
        "полный фактический список SKU",
        "точное количество каждого SKU",
        "правильное общее количество",
        "всё количество на одну SKU",
        "синхронизации и обработке движения товара",
        "учёта перемещения",
        "списания остатков ФФ",
        "расчёта себестоимости",
        "условную SKU",
    ):
        _assert(required in text, f"warehouse instruction contract missing: {required}")
    for jargon in ("API", "registry", "payload", "acceptance/options"):
        _assert(jargon not in text, f"manager instruction must not contain technical jargon: {jargon}")

    actual_name_tokens: set[str] = set()
    for warehouse in CENTRAL_STORAGE_WAREHOUSES:
        for raw_name in (warehouse.canonical_name, *warehouse.aliases):
            actual_name_tokens.update(
                token.casefold()
                for token in re.findall(r"[A-Za-zА-Яа-яЁё]+", raw_name)
                if len(token) >= 4
            )
    normalized_text = text.casefold()
    leaked = sorted(token for token in actual_name_tokens if token in normalized_text)
    _assert(not leaked, f"published section must not duplicate warehouse registry names: {leaked}")


def _assert_operator_ui_label_contract(instruction) -> None:
    section = next(section for section in instruction.sections if section.anchor == "wb-warehouse-selection")
    content = _section_text(section)
    operator_template = (ROOT / "packages/adapters/templates/sheet_vitrina_v1_operator.html").read_text(encoding="utf-8")
    planning_backend = (ROOT / "packages/application/wb_regional_supply_planning.py").read_text(encoding="utf-8")
    _assert_button_label(operator_template, 'data-tab-button="factory-order"', "Поставки")
    _assert_button_label(operator_template, 'data-supply-mode-button="calculations"', "Расчёты")
    _assert_button_label(operator_template, 'data-supply-section-button="regional"', "Поставка на Wildberries")
    _assert_button_label(operator_template, 'id="regionalDistrictExcludeFarSiberiaButton"', WB_WAREHOUSE_SELECTION_EXCLUDE_LABEL)
    _assert_button_label(operator_template, 'id="calculateRegionalSupplyButton"', WB_WAREHOUSE_SELECTION_CALCULATE_LABEL)
    _assert(f': "{WB_WAREHOUSE_SELECTION_PLAN_LABEL}";' in operator_template, "planning button rename must fail the instruction contract")
    _assert(f'"{WB_WAREHOUSE_SELECTION_RECOMMENDATION_LABEL}"' in planning_backend, "recommendation label rename must fail the instruction contract")
    _assert('"<td><strong>#" + escapeHtml(valueOrDash(item.rank))' in operator_template, "rank #1 rendering drift must fail the instruction contract")
    _assert(WB_WAREHOUSE_SELECTION_ROUTE in content, "instruction route must use the current operator UI labels")


def _assert_safe_rendering(instruction) -> None:
    dangerous_block = InstructionBlock(
        block_id="escape-test",
        kind="important",
        title="<img src=x onerror=alert(1)>",
        text="<script>alert(1)</script>",
    )
    rendered_block = _render_operator_instruction_block(
        dangerous_block,
        is_new=True,
        badge_label='<svg onload="alert(2)">',
    )
    _assert("<script>" not in rendered_block and "&lt;script&gt;" in rendered_block, "block text must be escaped")
    _assert("<img" not in rendered_block and "&lt;img" in rendered_block, "block title must be escaped")
    _assert("<svg" not in rendered_block and "&lt;svg" in rendered_block, "badge label must be escaped")
    rendered_badge = _render_instruction_new_badge('<b title="unsafe">NEW</b>')
    _assert("<b" not in rendered_badge and "&lt;b" in rendered_badge, "standalone badge label must be escaped")

    update = PUBLISHED_OPERATOR_INSTRUCTION_UPDATES[0]
    dangerous_update = replace(
        update,
        update_id='unsafe-update"><img src=x>',
        summary="<script>update()</script>",
        revisit_condition="<svg onload=update()>",
    )
    dangerous_instruction = replace(instruction, title="<img src=x>")
    rendered_update = _render_operator_instruction_update_item(dangerous_update, dangerous_instruction)
    _assert("<script>" not in rendered_update and "&lt;script&gt;" in rendered_update, "update summary must be escaped")
    _assert("<svg" not in rendered_update and "&lt;svg" in rendered_update, "update revisit condition must be escaped")
    _assert(rendered_update.count("<img") == 0 and "&lt;img" in rendered_update, "update ids and titles must be escaped")


def _section_text(section) -> str:
    values = [section.title, section.lead]
    for block in section.blocks:
        values.extend((block.title, block.text, *block.items, *block.headers))
        values.extend(cell for row in block.rows for cell in row)
    return "\n".join(value for value in values if value)


def _assert_button_label(source: str, attribute: str, label: str) -> None:
    pattern = r"<button\b(?=[^>]*" + re.escape(attribute) + r")[^>]*>\s*" + re.escape(label) + r"\s*</button>"
    _assert(re.search(pattern, source) is not None, f"operator UI button label drifted: {attribute} -> {label}")


def _expect_value_error(callable_, message: str) -> None:
    try:
        callable_()
    except ValueError:
        return
    raise AssertionError(message)


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


if __name__ == "__main__":
    main()
