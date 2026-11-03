"""Inspect explicit local 583 artifacts; this command cannot complete History."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from packages.application.fbs_583_history_completion_preflight import inspect_case,PreflightEvidenceError


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case-manifest',type=Path,required=True)
    parser.add_argument('--now',required=True,help='Aware inspection clock; never an execution clock override')
    args=parser.parse_args(argv)
    try:
        report=inspect_case(args.case_manifest,now=args.now)
    except (PreflightEvidenceError,OSError,ValueError,KeyError,TypeError) as exc:
        code=str(exc) if isinstance(exc,PreflightEvidenceError) else type(exc).__name__
        print(json.dumps(dict(status='evidence_blocked',execution_supported=False,ready=False,error_code=code),sort_keys=True))
        return 2
    print(json.dumps(report,ensure_ascii=False,sort_keys=True,indent=2))
    return 2 if report['conflicts'] else 0


if __name__=='__main__':raise SystemExit(main())
