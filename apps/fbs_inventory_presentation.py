#!/usr/bin/env python3
"""Read-only FF/capital preview using an explicit saved FBS candidate."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from packages.application.fbs_inventory_presentation import capture_inventory_snapshot


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--fbs-state', type=Path, required=True)
    parser.add_argument('--day', required=True)
    parser.add_argument('--wb-version')
    args=parser.parse_args()
    snapshot=capture_inventory_snapshot(args.db, fbs_state=json.loads(args.fbs_state.read_text()),
                                        day=args.day, wb_version_id=args.wb_version)
    print(json.dumps({'candidate_only':True, 'snapshot':snapshot.payload(),
        'warehouse':snapshot.warehouse_detail(), 'planning':snapshot.planning_payload(),
        'total_metrics':snapshot.metrics()},ensure_ascii=False,indent=2))


if __name__=='__main__':main()
