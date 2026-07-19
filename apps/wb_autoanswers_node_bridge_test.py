#!/usr/bin/env python3
"""Free Python↔Node contract checks; no OpenAI call is possible in fixture mode."""

from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from packages.application.wb_autoanswers_node_bridge import (
    NodeAutoanswersBridge,
    build_frozen_raw_input,
)
from packages.contracts.wb_autoanswers import EVALUATION_SIGNATURE, PROMPT_BUNDLE_VERSION


class NodeBridgeTest(unittest.TestCase):
    def bridge(self) -> NodeAutoanswersBridge:
        return NodeAutoanswersBridge(env={**os.environ, "WB_AUTOANSWERS_TEST_MODE": "1"})

    def test_frozen_identity_verification(self) -> None:
        result = self.bridge().verify()
        self.assertEqual(result["verified"]["artifact_count"], 28)

    def test_fixture_pipeline_crosses_versioned_json_boundary(self) -> None:
        raw = {
            "ingestion_id": "python-node-1",
            "review_id": "python-node-1",
            "review_version": "1",
            "rating": 3,
            "text": "После установки остались небольшие пузыри",
            "pros": "",
            "cons": "",
            "seller_article": "(Anti-Spy) iPhone 14 Pro",
            "nm_id": 428849827,
            "media": {"photos": [], "video": {"present": False}},
        }
        data = self.bridge().run(
            processing_key="python-node-1|1|1.4.2",
            raw_input=raw,
            execution_mode="fixture",
            fixture_scenario="public_only",
        )
        pipeline = data["pipeline"]
        self.assertEqual(pipeline["result"]["route"], "public_only")
        self.assertEqual(pipeline["result"]["versions"]["classifier_prompt"], PROMPT_BUNDLE_VERSION)
        self.assertGreater(len(data["audit"]), 0)

    def test_empty_five_star_skips_before_role_call(self) -> None:
        raw = {
            "ingestion_id": "empty-five",
            "review_id": "empty-five",
            "review_version": "1",
            "rating": 5,
            "text": "",
            "pros": "",
            "cons": "",
            "media": {"photos": [], "video": {"present": False}},
        }
        data = self.bridge().run(
            processing_key="empty-five|1|1.4.2",
            raw_input=raw,
            execution_mode="fixture",
            fixture_scenario="public_only",
        )
        self.assertEqual(data["pipeline"]["result"]["outcome"], "skipped")
        self.assertEqual(data["pipeline"]["model_calls_this_run"], 0)

    def test_server_adapter_embeds_downloaded_photo_and_marks_unknown_sku(self) -> None:
        with TemporaryDirectory() as directory:
            photo = Path(directory) / "photo.jpg"
            photo.write_bytes(b"not-a-real-image-but-bounded")
            detail = {
                "id": "unknown",
                "createdDate": "2026-07-20T00:00:00Z",
                "productValuation": 2,
                "text": "Не подошло",
                "pros": "",
                "cons": "",
                "tags": [],
                "content_version": 1,
                "productDetails": {
                    "nm_id": 999999999,
                    "supplier_article": "UNKNOWN-SKU",
                    "product_name": "Неизвестный товар",
                },
                "answer": {"text": ""},
                "media": [
                    {
                        "kind": "photo",
                        "fetch_status": "downloaded",
                        "local_path": str(photo),
                        "source_full_url": "https://cdn.example/photo.jpg?temporary=1",
                        "source_preview_url": "",
                    }
                ],
            }
            raw = build_frozen_raw_input(detail, processing_key="unknown|1|1.4.2")
            self.assertTrue(raw["media"]["photos"][0]["local_ref"].startswith("data:image/jpeg;base64,"))
            self.assertEqual(raw["seller_article"], "UNKNOWN-SKU")
            self.assertNotIn("line", raw)
            self.assertEqual(EVALUATION_SIGNATURE[:7], "sha256:")


if __name__ == "__main__":
    unittest.main(verbosity=2)
