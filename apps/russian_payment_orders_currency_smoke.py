"""Text-only regressions for the payment amount currency boundary."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.russian_payment_orders import parse_russian_payment_order_text  # noqa: E402
from packages.contracts.russian_payment_orders import (  # noqa: E402
    RUSSIAN_PAYMENT_ORDER_PARSE_STATUS_NEEDS_REVIEW,
    RUSSIAN_PAYMENT_ORDER_PARSE_STATUS_PARSED,
)

SYNTHETIC_SHA = "sha256:" + "0" * 64


def _assert_amount_words_currency(wb_text: str, vtb_text: str) -> None:
    for text, original_words, original_amount in (
        (wb_text, "Двенадцать тысяч триста сорок пять рублей 67 копеек", "12345-67"),
        (vtb_text, "Двадцать три тысячи четыреста пятьдесят шесть рублей 78 копеек", "23456-78"),
    ):
        for words, amount in (
            ("Одиннадцать рублей 00 копеек", "11-00"),
            ("Сто восемнадцать тысяч девятьсот одиннадцать рублей 00 копеек", "118911-00"),
            ("ОДИННАДЦАТЬ ТЫСЯЧ РУБЛЕЙ 00 КОПЕЕК", "11000-00"),
        ):
            candidate = text.replace(original_words, words).replace(original_amount, amount)
            parsed = parse_russian_payment_order_text(candidate, file_sha256=SYNTHETIC_SHA)
            if (
                parsed["currency"] != "RUB"
                or parsed["amount"] != amount.replace("-", ".")
                or parsed["parse_status"] != RUSSIAN_PAYMENT_ORDER_PARSE_STATUS_PARSED
                or not parsed["posting_eligible"]
            ):
                raise AssertionError(f"amount words must not act as an INN label: {words}")

        # Rubles after the actual INN field cannot prove the amount's currency.
        no_currency = text.replace(original_words, "Одиннадцать условных единиц")
        no_currency = no_currency.replace("Назначение платежа", "рублей\nНазначение платежа")
        parsed = parse_russian_payment_order_text(no_currency, file_sha256=SYNTHETIC_SHA)
        assert parsed["parse_status"] == RUSSIAN_PAYMENT_ORDER_PARSE_STATUS_NEEDS_REVIEW
        assert not parsed["posting_eligible"]
        if parsed["currency"] or "critical field needs review: currency" not in parsed["warnings"]:
            raise AssertionError("currency must remain unproven outside the amount words")


if __name__ == "__main__":
    fixtures = ROOT / "apps" / "fixtures" / "russian_payment_orders"
    _assert_amount_words_currency(
        (fixtures / "wb_bank_0401060.txt").read_text(encoding="utf-8"),
        (fixtures / "vtb_0401060.txt").read_text(encoding="utf-8"),
    )
    print("russian_payment_orders_currency_smoke: ok")
