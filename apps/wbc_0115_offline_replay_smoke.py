#!/usr/bin/env python3
"""Run the standalone buyer-support suite through the repository CI hook."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.discover(str(ROOT / "apps/buyer_support_bot_tests"))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)
