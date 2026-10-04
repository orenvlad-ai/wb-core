#!/usr/bin/env python3
"""One-shot local buyer-support observation sync. WB GET only; no scheduling."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.wb_buyer_support import WbBuyerSupportReadAdapter, BuyerSupportApiError
from packages.application.wb_buyer_support import BuyerSupportRepository, BuyerSupportSync


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-dir', required=True, type=Path)
    parser.add_argument('--cabinet', default=os.environ.get('WB_BUYER_SUPPORT_CABINET_ID', ''))
    parser.add_argument('--token-env-var', default='WB_API_TOKEN')
    # Rate class is a required operator choice; never assume the cabinet has a fast token.
    parser.add_argument('--token-rate-class', required=True, choices=('personal-service', 'base'))
    parser.add_argument('--max-pages', type=int, default=1000)
    args = parser.parse_args()
    if not args.cabinet:
        parser.error('--cabinet or WB_BUYER_SUPPORT_CABINET_ID is required')
    pause = 3601 if args.token_rate_class == 'base' else 3.1
    try:
        result = BuyerSupportSync(BuyerSupportRepository(args.runtime_dir),
                                 WbBuyerSupportReadAdapter(token_env_var=args.token_env_var),
                                 page_pause_seconds=pause).run(args.cabinet, max_pages=args.max_pages)
    except BuyerSupportApiError as exc:
        print(json.dumps({'status': 'error', 'code': exc.code, 'http_status': exc.http_status}))
        return 1
    except Exception:
        # Upstream/local exception strings can contain sensitive content; never print them.
        print(json.dumps({'status': 'error', 'code': 'observation_sync_failed'}))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
