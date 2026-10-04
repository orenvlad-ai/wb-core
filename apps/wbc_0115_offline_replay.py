#!/usr/bin/env python3
"""Standalone offline replay; no WB services imported or started."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from packages.domain.buyer_support_bot.replay import main

if __name__ == "__main__":
    raise SystemExit(main())
