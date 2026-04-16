"""Targeted smoke-check for percent formatting in sheet_vitrina_v1 presentation."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
PRESENTATION_PASS_PATH = ROOT / "gas" / "sheet_vitrina_v1" / "PresentationPass.gs"


def main() -> None:
    payload = json.loads(
        subprocess.check_output(
            ["node", "-e", _node_program()],
            cwd=ROOT,
            text=True,
        )
    )

    for metric_key in ("ads_cr", "avg_ads_cr", "ads_ctr"):
        if payload[metric_key] != "0.0%":
            raise AssertionError(f"{metric_key} must keep percent pattern, got {payload[metric_key]!r}")
    if payload["ads_sum"] != "#,##0.00":
        raise AssertionError(f"ads_sum must keep decimal currency pattern, got {payload['ads_sum']!r}")

    print("percent_pattern: ok -> ads_cr + avg_ads_cr")
    print("regression_guard: ok -> ads_ctr stays percent, ads_sum stays decimal")
    print("smoke-check passed")


def _node_program() -> str:
    path = json.dumps(str(PRESENTATION_PASS_PATH))
    return f"""
const fs = require('fs');
const vm = require('vm');

const code = fs.readFileSync({path}, 'utf8');
const context = {{ console }};
vm.createContext(context);
vm.runInContext(code, context);

const payload = {{
  ads_cr: context.resolveDataPattern_('ads_cr'),
  avg_ads_cr: context.resolveDataPattern_('avg_ads_cr'),
  ads_ctr: context.resolveDataPattern_('ads_ctr'),
  ads_sum: context.resolveDataPattern_('ads_sum'),
}};

process.stdout.write(JSON.stringify(payload));
"""


if __name__ == "__main__":
    main()
