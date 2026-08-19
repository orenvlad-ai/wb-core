"""Synthetic smoke for the isolated Russian payment-order parser."""

from __future__ import annotations

from contextlib import redirect_stderr
from io import BytesIO, StringIO
import json
from pathlib import Path
import sys

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.russian_payment_orders import (  # noqa: E402
    parse_russian_payment_order_pdf,
    parse_russian_payment_order_text,
)
from packages.contracts.russian_payment_orders import (  # noqa: E402
    RUSSIAN_PAYMENT_ORDER_ADAPTER_VTB,
    RUSSIAN_PAYMENT_ORDER_ADAPTER_WB_BANK,
    RUSSIAN_PAYMENT_ORDER_PARSE_STATUS_NEEDS_REVIEW,
    RUSSIAN_PAYMENT_ORDER_PARSE_STATUS_PARSED,
    RUSSIAN_PAYMENT_ORDER_PARSE_STATUS_PARSE_ERROR,
    RUSSIAN_PAYMENT_ORDER_PARSER_VERSION,
    RUSSIAN_PAYMENT_ORDER_VAT_NOT_TAXED,
    RUSSIAN_PAYMENT_ORDER_VAT_TAXED,
)


FIXTURE_DIR = ROOT / "apps" / "fixtures" / "russian_payment_orders"
SYNTHETIC_SHA = "sha256:" + "0" * 64


def main() -> None:
    wb_text = _fixture("wb_bank_0401060.txt")
    wb_equivalent_text = _fixture("wb_bank_0401060_equivalent_layout.txt")
    vtb_text = _fixture("vtb_0401060.txt")

    wb_pdf = _render_pdf(wb_text, title="synthetic-wb-layout-a", x_offset=0)
    wb_equivalent_pdf = _render_pdf(
        wb_equivalent_text,
        title="synthetic-wb-layout-b",
        x_offset=24,
    )
    vtb_pdf = _render_pdf(vtb_text, title="synthetic-vtb-layout", x_offset=8)

    wb = parse_russian_payment_order_pdf(wb_pdf, filename="synthetic-layout-a.pdf")
    wb_equivalent = parse_russian_payment_order_pdf(
        wb_equivalent_pdf,
        filename="renamed-equivalent-layout.pdf",
    )
    vtb = parse_russian_payment_order_pdf(vtb_pdf, filename="synthetic-vtb.pdf")

    _assert_wb(wb)
    _assert_wb(wb_equivalent)
    _assert_vtb(vtb)
    if wb["file_sha256"] == wb_equivalent["file_sha256"]:
        raise AssertionError("different synthetic PDF layouts must keep distinct file SHA-256 values")
    if wb["payment_fingerprint"] != wb_equivalent["payment_fingerprint"]:
        raise AssertionError("filename and equivalent PDF layout must not change payment fingerprint")

    different = parse_russian_payment_order_text(
        wb_equivalent_text.replace("№101", "№102", 1),
        file_sha256=SYNTHETIC_SHA,
    )
    if (
        different["parse_status"] != RUSSIAN_PAYMENT_ORDER_PARSE_STATUS_PARSED
        or not different["payment_fingerprint"]
        or different["payment_fingerprint"] == wb["payment_fingerprint"]
    ):
        raise AssertionError("different payment-order identity must not collapse")

    corporate_kpp = parse_russian_payment_order_text(
        wb_text.replace("КПП 0 12345-67", "КПП 000000009 12345-67", 1),
        file_sha256=SYNTHETIC_SHA,
    )
    if (
        corporate_kpp["payer"]["kpp"] != "000000009"
        or corporate_kpp["payer"]["bank"]["bic"] != "040000001"
    ):
        raise AssertionError("nine-digit payer KPP must not be classified as a BIC")

    unsupported = parse_russian_payment_order_text(
        wb_text.replace("ВБ Банк", "Тест Банк"),
        file_sha256=SYNTHETIC_SHA,
    )
    _assert_fail_closed(unsupported, expected_status=RUSSIAN_PAYMENT_ORDER_PARSE_STATUS_PARSE_ERROR)

    not_executed = parse_russian_payment_order_text(
        wb_text.replace("ИСПОЛНЕН\n19.08.2026 10:11:12", "НЕ ИСПОЛНЕН"),
        file_sha256=SYNTHETIC_SHA,
    )
    _assert_fail_closed(not_executed, expected_status=RUSSIAN_PAYMENT_ORDER_PARSE_STATUS_NEEDS_REVIEW)
    if not_executed["execution_status"] != "not_executed":
        raise AssertionError("explicit non-execution marker must remain explicit")

    with redirect_stderr(StringIO()):
        damaged = parse_russian_payment_order_pdf(
            b"%PDF-synthetic-damaged",
            filename="damaged.pdf",
        )
    _assert_fail_closed(damaged, expected_status=RUSSIAN_PAYMENT_ORDER_PARSE_STATUS_PARSE_ERROR)

    serialized = json.dumps(wb, ensure_ascii=False, sort_keys=True)
    if "synthetic-layout-a.pdf" in serialized:
        raise AssertionError("filename must not leak into normalized result or fingerprint material")
    if wb["parser_version"] != RUSSIAN_PAYMENT_ORDER_PARSER_VERSION:
        raise AssertionError("parser version changed")
    print("russian_payment_orders_smoke: ok")


def _assert_wb(parsed: dict[str, object]) -> None:
    _assert_common_contract(parsed)
    if parsed["parse_status"] != RUSSIAN_PAYMENT_ORDER_PARSE_STATUS_PARSED:
        raise AssertionError(f"synthetic WB Bank order must parse: {parsed['warnings']}")
    if not parsed["posting_eligible"] or parsed["adapter"] != RUSSIAN_PAYMENT_ORDER_ADAPTER_WB_BANK:
        raise AssertionError("executed WB Bank order must be eligible and adapter-bound")
    reference = parsed["invoice_reference"]
    vat = parsed["vat"]
    if not isinstance(reference, dict) or reference != {"number": "SYN-101", "date": ""}:
        raise AssertionError(f"WB invoice wording changed: {reference}")
    if not isinstance(vat, dict) or vat != {
        "status": RUSSIAN_PAYMENT_ORDER_VAT_TAXED,
        "rate_percent": "5",
        "amount": "587.89",
    }:
        raise AssertionError(f"WB VAT wording changed: {vat}")
    if parsed["warnings"] != ["invoice reference date is not present"]:
        raise AssertionError(f"optional WB invoice date warning changed: {parsed['warnings']}")


def _assert_vtb(parsed: dict[str, object]) -> None:
    _assert_common_contract(parsed)
    if parsed["parse_status"] != RUSSIAN_PAYMENT_ORDER_PARSE_STATUS_PARSED:
        raise AssertionError(f"synthetic VTB order must parse: {parsed['warnings']}")
    if not parsed["posting_eligible"] or parsed["adapter"] != RUSSIAN_PAYMENT_ORDER_ADAPTER_VTB:
        raise AssertionError("executed VTB order must be eligible and adapter-bound")
    reference = parsed["invoice_reference"]
    vat = parsed["vat"]
    if not isinstance(reference, dict) or reference != {
        "number": "SYN-202",
        "date": "2026-08-20",
    }:
        raise AssertionError(f"VTB invoice wording changed: {reference}")
    if not isinstance(vat, dict) or vat != {
        "status": RUSSIAN_PAYMENT_ORDER_VAT_NOT_TAXED,
        "rate_percent": "",
        "amount": "",
    }:
        raise AssertionError(f"VTB VAT wording changed: {vat}")


def _assert_fail_closed(parsed: dict[str, object], *, expected_status: str) -> None:
    if parsed["parse_status"] != expected_status or parsed["posting_eligible"]:
        raise AssertionError(f"document did not fail closed: {parsed}")


def _assert_common_contract(parsed: dict[str, object]) -> None:
    payer = parsed["payer"]
    beneficiary = parsed["beneficiary"]
    if not isinstance(payer, dict) or not isinstance(beneficiary, dict):
        raise AssertionError("normalized parties are missing")
    payer_bank = payer.get("bank")
    beneficiary_bank = beneficiary.get("bank")
    required = [
        parsed.get("form_code"),
        parsed.get("source_bank"),
        parsed.get("payment_order_number"),
        parsed.get("document_date"),
        parsed.get("debit_date"),
        parsed.get("execution_date"),
        parsed.get("executed_at"),
        parsed.get("amount"),
        parsed.get("currency"),
        parsed.get("payment_purpose"),
        parsed.get("payment_fingerprint"),
        parsed.get("file_sha256"),
        payer.get("name"),
        payer.get("inn"),
        payer.get("account"),
        beneficiary.get("name"),
        beneficiary.get("inn"),
        beneficiary.get("account"),
    ]
    if not isinstance(payer_bank, dict) or not isinstance(beneficiary_bank, dict):
        raise AssertionError("normalized party banks are missing")
    required.extend(
        [
            payer_bank.get("name"),
            payer_bank.get("bic"),
            payer_bank.get("correspondent_account"),
            beneficiary_bank.get("name"),
            beneficiary_bank.get("bic"),
            beneficiary_bank.get("correspondent_account"),
        ]
    )
    if not all(required):
        raise AssertionError(f"normalized common contract is incomplete: {parsed}")


def _fixture(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


def _render_pdf(text: str, *, title: str, x_offset: int) -> bytes:
    font_path = _font_path()
    font_name = "RussianPaymentOrderSynthetic"
    if font_name not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(font_name, str(font_path)))
    stream = BytesIO()
    document = canvas.Canvas(stream, pagesize=(900, 1400), pageCompression=1)
    document.setTitle(title)
    document.setFont(font_name, 9)
    y = 1360
    for line in text.splitlines():
        document.drawString(24 + x_offset, y, line)
        y -= 18
        if y < 36:
            document.showPage()
            document.setFont(font_name, 9)
            y = 1360
    document.save()
    return stream.getvalue()


def _font_path() -> Path:
    candidates = (
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
        Path("/Library/Fonts/Arial Unicode.ttf"),
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise RuntimeError("no synthetic-fixture Cyrillic TrueType font is available")


if __name__ == "__main__":
    main()
