"""Local scheduled wiring/guard proof; no native source reads or production launch."""
from contextlib import redirect_stdout
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import json
import shlex
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps import web_vitrina_history_candidate_build as command
from apps.web_vitrina_history_live_smoke import check_cli_guards


def main():
    repo = Path(__file__).resolve().parents[1]
    contract_path = repo / 'artifacts/registry_upload_http_entrypoint/input/web_vitrina_history_runtime.json'
    contract = json.loads(contract_path.read_text())
    root = Path(contract['candidate_root'])
    reserve = 8 * 1024**3 + 2 * 1024**3 + 128 * 1024**2
    assert contract['reserve_bytes'] == reserve
    epoch = contract['formula_epoch']
    with patch.object(command.os.path, 'ismount', return_value=True), \
            patch.object(command.subprocess, 'check_output', return_value=contract['mount']), \
            patch.object(command.os, 'statvfs', return_value=SimpleNamespace(f_bavail=reserve, f_frsize=1)):
        command.runtime_storage_admission(root, contract_path, epoch)
        for changed, expected in [('reserve', 'history_storage_reserve'),
                                  ('mount', 'history_mount_identity_drift'),
                                  ('epoch', 'history_formula_epoch_drift')]:
            with patch.object(command.os, 'statvfs', return_value=SimpleNamespace(
                    f_bavail=reserve - (changed == 'reserve'), f_frsize=1)), \
                    patch.object(command.subprocess, 'check_output', return_value=(
                        contract['mount'] + ',unexpected' if changed == 'mount' else contract['mount'])):
                try:
                    command.runtime_storage_admission(root, contract_path,
                        'installed-release-sha' if changed == 'epoch' else epoch)
                except ValueError as error:
                    assert str(error) == expected
                else:
                    raise AssertionError(changed + ' was admitted')
        with patch.object(command.hashlib, 'sha256', wraps=command.hashlib.sha256) as hashing:
            # A formula-code change refuses reuse of the accepted epoch.
            with patch.object(Path, 'read_bytes', return_value=b'changed formula'):
                try:
                    command.runtime_storage_admission(root, contract_path, epoch)
                except ValueError as error:
                    assert str(error) == 'history_formula_code_drift'
                else:
                    raise AssertionError('formula drift admitted')
            assert hashing.called
    service = (repo / 'artifacts/registry_upload_http_entrypoint/systemd/wb-core-web-vitrina-finished-snapshot.service').read_text()
    http = (repo / 'artifacts/registry_upload_http_entrypoint/systemd/wb-core-registry-http.service').read_text()
    exec_start = next(line[len('ExecStart='):] for line in service.splitlines() if line.startswith('ExecStart='))
    args = shlex.split(exec_start)[2:]
    assert '--manual' not in args and args[args.index('--date-from') + 1] == '2026-03-01'
    assert args[args.index('--date-to') + 1] == 'business-today'
    assert args[args.index('--formula-epoch') + 1] == epoch
    assert args[args.index('--candidate-root') + 1] == str(root)
    assert 'Environment=WEB_VITRINA_HISTORY_STORE=' + str(root / 'history') in http
    with tempfile.TemporaryDirectory() as temporary:
        fixture = Path(temporary)
        check_cli_guards(fixture)
        runtime = fixture / 'runtime'
        arguments = ['candidate', '--runtime-dir', str(runtime), '--candidate-root', str(root),
                     '--date-from', '2026-03-01', '--date-to', 'business-today',
                     '--formula-epoch', epoch, '--runtime-contract', str(contract_path)]
        # Missing mount rejects before singleflight/root creation or child launch.
        output = StringIO()
        with patch.object(sys, 'argv', arguments), patch.object(command, 'deadline_seconds', return_value=10), \
                patch.object(command, 'admission', return_value='idle'), \
                patch.object(command.os.path, 'ismount', return_value=False), \
                patch.object(command, 'candidate_singleflight') as candidate_lock, \
                patch.object(command, 'bounded_worker') as worker, redirect_stdout(output):
            command.main()
            assert json.loads(output.getvalue())['status'] == 'skipped_storage'
            assert not candidate_lock.called and not worker.called
        # At UTC20:00 the business date is already next day, independently of
        # the quiet slot timezone. Freeze that date/now into the child argv.
        now = datetime(2026, 10, 3, 20, 0, tzinfo=timezone.utc)
        arguments[arguments.index('--candidate-root') + 1] = str(fixture / 'scheduled')
        with patch.object(sys, 'argv', arguments), patch.object(command, 'datetime') as clock, \
                patch.object(command, 'deadline_seconds', side_effect=[10, 7]), \
                patch.object(command, 'admission', return_value='idle'), \
                patch.object(command, 'runtime_storage_admission') as storage, \
                patch.object(command, 'bounded_worker', return_value={'status': 'fixture_only'}) as worker, \
                redirect_stdout(StringIO()):
            clock.now.return_value = now
            command.main()
            argv, seconds = worker.call_args.args
            assert argv[-5:] == ['--date-to', '2026-10-04', '--captured-now', now.isoformat(), '--worker']
            assert seconds == 7 and storage.called
        with patch.object(sys, 'argv', arguments), patch.object(command, 'deadline_seconds', return_value=7), \
                patch.object(command, 'admission', return_value='idle'), \
                patch.object(command, 'runtime_storage_admission'), \
                patch.object(command, 'bounded_worker', return_value={'status': 'build_failed'}), \
                redirect_stdout(StringIO()):
            assert command.main() == 1
    print(json.dumps({'status': 'pass', 'fixture_only': True,
        'mount_reserve_formula_guards': True, 'missing_mount_before_writes': True,
        'current_business_date_pinned': True, 'existing_admission_shared_lock_hard_budget': True,
        'failed_child_nonzero_service_exit': True,
        'stable_reader_builder_path': str(root / 'history')}))


if __name__ == '__main__':
    main()
