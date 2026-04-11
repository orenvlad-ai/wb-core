"""Smoke-check для pilot registry bundle."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_pilot_bundle import (
    load_registry_pilot_bundle,
    validate_registry_pilot_bundle,
)


def main() -> None:
    bundle = load_registry_pilot_bundle()
    report = validate_registry_pilot_bundle(bundle)
    print("bundle: ok -> consistency")
    print(f"config_v2: count -> {report.sku_count}")
    print(f"metrics_v2: count -> {report.metric_count}")
    print(f"formulas_v2: count -> {report.formula_count}")
    print(f"metric_runtime_registry: count -> {report.runtime_metric_count}")
    print(f"metric_kinds: ok -> {', '.join(report.metric_kinds)}")
    print(f"bridge_export: ok -> {len(report.bridge_paths)} paths")
    print("smoke-check passed")


if __name__ == "__main__":
    main()
