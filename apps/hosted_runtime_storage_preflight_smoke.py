#!/usr/bin/env python3
"""Run the generated readonly preflight against an installed-checker fixture."""
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps import registry_upload_http_entrypoint_hosted_runtime as hosted


def main():
    with TemporaryDirectory() as raw:
        root = Path(raw)
        (root / 'apps').mkdir()
        checker = root / 'apps/root_storage_policy.py'
        target = replace(hosted.load_hosted_runtime_target(ROOT / 'artifacts/registry_upload_http_entrypoint/input/hosted_runtime_target__europe_api.json'), target_dir=str(root))
        commands = hosted._build_root_storage_policy_commands(target)
        assert commands['status_artifact_readback']
        shell = commands['preflight'][-1]
        good = {'unregistered_large_root_files': [], 'storage_registry': {'ok': True}, 'alerts': []}
        cases = [(good, 0), ({**good, 'storage_registry': {'ok': False}, 'alerts': [{'code': 'storage_reserve_breach'}]}, 3),
                 ({**good, 'unregistered_large_root_files': ['unknown']}, 3),
                 ({**good, 'storage_registry': {'ok': 'yes'}}, 3),
                 ({'storage_registry': {'ok': True}}, 3), ({}, 3), ([], 3)]
        for payload, expected in cases:
            checker.write_text("import sys\nassert sys.argv[-1] == 'status'\nassert '--output' not in sys.argv\nprint(" + repr(json.dumps(payload)) + ")\n")
            result = subprocess.run(['sh', '-c', shell], capture_output=True, text=True)
            assert result.returncode == expected, (payload, result.stderr)
        for body in ("print('broken JSON')", "import sys; sys.exit(2)"):
            checker.write_text(body)
            result = subprocess.run(['sh', '-c', shell], capture_output=True, text=True)
            assert result.returncode != 0
        assert sorted(p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file()) == ['apps/root_storage_policy.py']
    print('readonly storage preflight: PASS')


if __name__ == '__main__':
    main()
