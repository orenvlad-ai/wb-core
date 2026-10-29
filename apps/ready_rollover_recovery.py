#!/usr/bin/env python3
"""Preview/apply one exact, witnessed lost ready column. No source collection."""
import argparse
from datetime import datetime, timezone
import json
import os
from types import SimpleNamespace
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from packages.application.storage_registry import StoreRegistry
from packages.application import ready_rollover_recovery as recovery


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-dir', required=True)
    sub = parser.add_subparsers(dest='command', required=True)
    preview = sub.add_parser('preview')
    preview.add_argument('--cycle-id', required=True)
    preview.add_argument('--date', required=True)
    preview.add_argument('--output', required=True)
    apply = sub.add_parser('apply')
    apply.add_argument('--reviewed', required=True)
    apply.add_argument('--operation-id', required=True)
    args = parser.parse_args()
    root = Path(args.runtime_dir).resolve()
    runtime = SimpleNamespace(runtime_dir=root, db_path=StoreRegistry(root).resolve("operational"))
    if args.command == 'preview':
        result = recovery.preview(runtime, cycle_id=args.cycle_id, day=args.date)
        # Exclusive creation prevents accidental replacement of a reviewed artifact.
        with os.fdopen(os.open(args.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'w') as stream:
            json.dump(result, stream, ensure_ascii=False, sort_keys=True)
        print(json.dumps({'status':'preview','as_of_date':args.date,'preview_digest':result['preview_digest'],
            'book_version':result['book_version'],'source_as_of_date':result['candidate']['donor']['as_of_date'],
            'source_refreshed_at':result['candidate']['refreshed_at'],'cost_quality':'unavailable'},ensure_ascii=False))
    else:
        path = Path(args.reviewed)
        if path.stat().st_size > 40*1024**2:
            raise ValueError('ready_rollover_recovery_review_limit')
        result = recovery.apply(runtime, reviewed=json.loads(path.read_text()), operation_id=args.operation_id,
            finished_at=datetime.now(timezone.utc).isoformat().replace('+00:00','Z'))
        print(json.dumps(result,ensure_ascii=False))

if __name__ == '__main__':
    main()
