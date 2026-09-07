#!/usr/bin/env python3
"""Exact candidate activation, drift rejection and journaled one-submit recovery."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from unittest.mock import patch
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps.fbs_accounting_runtime_smoke import RuntimeTests
from packages.application import fbs_accounting_apply as apply
from packages.application.fbs_snapshot_cost import fingerprint
from apps.production_apply_launcher import execute


class ActivationTests(RuntimeTests):
    def prepared(self):
        from packages.application.fbs_accounting_runtime import prepare
        book, _ = prepare(self.root, now=self.now, opening=True)
        file = self.root / "candidate.json"
        file.write_text(json.dumps(book))
        return book, {"runtime_dir": str(self.root), "candidate_path": str(file), "candidate_sha256": fingerprint(book)}

    def test_launcher_apply_and_readback_and_pause(self):
        book, request = self.prepared()
        adapter = apply.FbsAccountingAdapter()
        with patch.object(apply, "target", return_value=self.root), patch.object(apply, "prepare", return_value=(book, None)), patch.object(apply, "datetime") as clock:
            clock.fromisoformat.side_effect = datetime.fromisoformat
            clock.now.return_value = self.now
            preview = adapter.preview(request, "activate-test")
            receipt = execute(action="apply", adapter_name="fbs", operation_id="activate-test", request=request,
                expected_prestate=preview["prestate_sha256"], expected_candidate=preview["candidate_sha256"], adapters={"fbs": adapter})
            self.assertEqual(receipt["state"], "applied")
            self.assertEqual(adapter.readback(request, "activate-test")["state"], "applied")
            paused = {**book, "active": False}
            request.update(action="pause", candidate_sha256=fingerprint(paused))
            preview = adapter.preview(request, "pause-test")
            adapter.apply(request, "pause-test", preview)
            self.assertFalse(adapter.readback(request, "pause-test")["active"])

    def test_tampered_cost_with_same_source_manifest_is_rejected(self):
        book, request = self.prepared()
        changed = deepcopy(book)
        changed["shared_days"][self.day]["rows"]["1"]["unit_cost_rub"] = "1"
        Path(request["candidate_path"]).write_text(json.dumps(changed))
        request["candidate_sha256"] = fingerprint(changed)
        with patch.object(apply, "target", return_value=self.root), patch.object(apply, "prepare", return_value=(book, None)), patch.object(apply, "datetime") as clock:
            clock.fromisoformat.side_effect = datetime.fromisoformat
            clock.now.return_value = self.now
            with self.assertRaisesRegex(ValueError, "source_changed"):
                apply.FbsAccountingAdapter().preview(request, "tamper-test")


if __name__ == "__main__":
    unittest.main()
