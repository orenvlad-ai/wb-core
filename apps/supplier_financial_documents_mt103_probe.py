"""Local regression probe for the real VTB MT103_2.pdf sample."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.supplier_financial_documents import parse_financial_document_pdf  # noqa: E402


EXPECTED_FIELDS: dict[str, Any] = {
    "document_type": "bank_transfer_application",
    "document_number": "2",
    "transfer_application_number": "2",
    "document_date": "2026-05-21",
    "execution_status": "Исполнен",
    "execution_time": "22.05.2026 00:43:03",
    "debit_account": "40802156616580000008",
    "currency": "CNY",
    "transfer_amount": 541962.5,
    "total_amount": 541962.5,
    "ordering_customer": "IE SAGITOV VLADISLAV RADIKOVICH",
    "payer_inn": "560912740163",
    "payer_country_code": "RU",
    "beneficiary_customer": "GUANGZHOU ZIFRIEND COMMUNICATE TECHNOLOGY CO., LTD",
    "beneficiary_account": "40807156200610034920",
    "beneficiary_bank_swift_bic": "VTBRCNSHXXX",
    "beneficiary_bank_clearing_code": "//CN767290000018",
    "beneficiary_bank_country": "CN",
    "payment_details": "CONTRACT 083/26 DD 13.05.2026",
    "contract_ref": "CONTRACT 083/26 DD 13.05.2026",
    "contract_number": "083/26",
    "contract_date": "2026-05-13",
    "charges_mode": "OUR",
    "sender_to_receiver_info": "/PYTR/GOD/",
}


def main() -> None:
    pdf_path = _find_mt103_pdf()
    if pdf_path is None:
        print("supplier_financial_documents_mt103_probe: SKIP real MT103_2.pdf not found")
        return
    payload = parse_financial_document_pdf(pdf_path.read_bytes(), filename=pdf_path.name)
    normalized = dict(payload.get("normalized_parse") or {})
    result = {key: normalized.get(key) for key in EXPECTED_FIELDS}
    result["beneficiary_bank"] = normalized.get("beneficiary_bank")
    result["warnings"] = payload.get("warnings") or []
    result["errors"] = payload.get("errors") or []
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    missing_or_wrong = {
        key: {"expected": expected, "actual": normalized.get(key)}
        for key, expected in EXPECTED_FIELDS.items()
        if normalized.get(key) != expected
    }
    critical_missing = [
        key
        for key in (
            "debit_account",
            "currency",
            "transfer_amount",
            "ordering_customer",
            "beneficiary_customer",
            "beneficiary_account",
            "payment_details",
            "contract_number",
            "contract_date",
            "charges_mode",
        )
        if not normalized.get(key)
    ]
    if payload.get("errors") or payload.get("warnings") or missing_or_wrong or critical_missing:
        raise AssertionError(
            "MT103_2 parser regression: "
            + json.dumps(
                {
                    "missing_or_wrong": missing_or_wrong,
                    "critical_missing": critical_missing,
                    "warnings": payload.get("warnings") or [],
                    "errors": payload.get("errors") or [],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    print("supplier_financial_documents_mt103_probe: OK")


def _find_mt103_pdf() -> Path | None:
    folder = Path.home() / "Desktop" / "Пакет документов"
    if not folder.exists() or not folder.is_dir():
        return None
    for path in folder.iterdir():
        if path.name.casefold() == "mt103_2.pdf" and path.is_file():
            return path
    return None


if __name__ == "__main__":
    main()
