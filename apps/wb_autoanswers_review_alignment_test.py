"""Offline regression cases for the public-review owner guard."""

from __future__ import annotations

import unittest

from packages.application.wb_autoanswers_owner_policy import (
    apply_owner_policy,
    classify_return_guard,
)


class ReviewAlignmentTest(unittest.TestCase):
    def test_installation_crack_is_eligible_without_video(self) -> None:
        decision = classify_return_guard({"text": "Стекло треснуло, пока устанавливал"})
        self.assertIn("installation_breakage_before_use", decision["hard_return_reasons"])
        result = apply_owner_policy(
            feedback_id="install-crack", rating=2,
            content_json={"text": "Стекло треснуло, пока устанавливал"},
            result={"final_route": "wb_return", "final_reply": "Здравствуйте. Оформите заявку на возврат через Wildberries."},
        )
        self.assertEqual(result["final_route"], "wb_return")
        later_use = classify_return_guard({"text": "Стекло треснуло при установке, потом уже пользовался телефоном"})
        self.assertIn("installation_breakage_before_use", later_use["hard_return_reasons"])
        for wording in ("Стекло треснуло в процессе наклейки", "При наклеивании стекло треснуло"):
            self.assertIn("installation_breakage_before_use", classify_return_guard({"text": wording})["hard_return_reasons"])

    def test_week_of_use_is_explanation_and_first_inspection_is_return(self) -> None:
        post_use = classify_return_guard({"text": "Неделю пользовался, стекло треснуло"})
        self.assertFalse(post_use["hard_return"])
        self.assertTrue(post_use["post_use_breakage"])
        result = apply_owner_policy(
            feedback_id="week-use", rating=2,
            content_json={"text": "Неделю пользовался, стекло треснуло"},
            result={"final_route": "wb_return", "final_reply": "Здравствуйте. Оформите заявку на возврат через Wildberries."},
        )
        self.assertEqual(result["final_route"], "public_only")
        self.assertTrue(result["final_reply"].startswith("Здравствуйте."))
        early = classify_return_guard({"text": "При первом осмотре после установки до начала использования увидел трещину"})
        self.assertIn("installation_breakage_before_use", early["hard_return_reasons"])
        negated = classify_return_guard({"text": "При установке стекло не треснуло, через неделю использования появилась трещина"})
        self.assertNotIn("installation_breakage_before_use", negated["hard_return_reasons"])
        intact = classify_return_guard({"text": "При первом осмотре до использования всё было целым, через неделю появилась трещина"})
        self.assertFalse(intact["hard_return"])
        absent = classify_return_guard({"text": "Трещин нет, только пузыри после установки."})
        self.assertFalse(absent["post_use_breakage"])
        self.assertFalse(absent["hard_return"])
        later = classify_return_guard({"text": "При установке трещин не было, через неделю использования появилась трещина"})
        self.assertNotIn("installation_breakage_before_use", later["hard_return_reasons"])
        self.assertTrue(later["post_use_breakage"])

    def test_failed_bubble_remedy_does_not_downgrade_return(self) -> None:
        failed = classify_return_guard({"text": "Пробовал приподнимать и разглаживать, пузыри остались"})
        self.assertIn("failed_installation_remedy", failed["hard_return_reasons"])
        untried = classify_return_guard({"text": "Не пробовал приподнимать, пузыри остались"})
        self.assertNotIn("failed_installation_remedy", untried["hard_return_reasons"])
        for wording in (
            "Не пробовал приподнимать, но разглаживал, пузыри остались",
            "Сначала не пробовал разглаживать, потом попробовал, пузыри остались",
        ):
            self.assertIn("failed_installation_remedy", classify_return_guard({"text": wording})["hard_return_reasons"])
        dust = classify_return_guard({"text": "Под уже наклеенным стеклом пылинка. Стикеров нет."})
        self.assertIn("installed_dust_without_sticker", dust["hard_return_reasons"])
        missing_sticker = classify_return_guard({"text": "Не положили стикер от пыли, из-за чего под стеклом осталась пылинка, которую уже не убрать"})
        self.assertIn("installed_dust_without_sticker", missing_sticker["hard_return_reasons"])
        before_installation = classify_return_guard({"text": "До установки не положили стикер от пыли, тряпочка есть"})
        self.assertNotIn("installed_dust_without_sticker", before_installation["hard_return_reasons"])
        self.assertIn("failed_installation_remedy", classify_return_guard({"text": "Попытался выдавить пузыри, не получается"})["hard_return_reasons"])
        self.assertIn("failed_installation_remedy", classify_return_guard({"text": "Стекло не клеилось, пытался наклеить повторно, ничего не получить"})["hard_return_reasons"])

    def test_mechanism_and_negated_damage_are_separate(self) -> None:
        self.assertIn("installation_mechanism_failure", classify_return_guard({"text": "Язычок установочного бокса не поддавался"})["hard_return_reasons"])
        self.assertIn("installation_mechanism_failure", classify_return_guard({"text": "Механизм установки не работает корректно"})["hard_return_reasons"])
        self.assertNotIn("installation_mechanism_failure", classify_return_guard({"text": "Механизм не сломан, проблема только в размере стекла"})["hard_return_reasons"])
        for wording in (
            "Стекло хорошее, без трещин; стикера нет",
            "Сколько ни пытался приклеить, не держится",
        ):
            decision = classify_return_guard({"text": wording})
            self.assertFalse(decision["post_use_breakage"])
            self.assertNotIn("installation_breakage_before_use", decision["hard_return_reasons"])


if __name__ == "__main__":
    unittest.main()
