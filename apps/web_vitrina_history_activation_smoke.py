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



def check_maintenance_seam():
    from packages.application.business_data_procedure_admission import initialize_admission, admission_idle
    from packages.application.business_data_write_barrier import STATE_FILENAME, SCHEMA_VERSION
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        runtime = root / "runtime"
        runtime.mkdir()
        initialize_admission(runtime)
        (runtime / ".web-vitrina-finished-builder.lock").write_bytes(b"fixture")
        arguments = ["candidate", "--runtime-dir", str(runtime), "--candidate-root", str(root / "derived"),
                     "--date-from", "2026-03-01", "--date-to", "2026-10-04", "--formula-epoch", "fixture"]
        from contextlib import contextmanager
        from packages.application import owned_history_worker as delegation
        from packages.application.business_data_heavy_admission import require_heavy_owner
        @contextmanager
        def assert_parent_lease(**kwargs):
            assert admission_idle(runtime) == {"ready": True, "idle": False, "reason": "admitted_writer_running"}
            assert require_heavy_owner(runtime).operation=='history'
            yield SimpleNamespace(complete=lambda *args, **kw: {'status':'fixture_only'})
        with patch.object(sys, "argv", [*arguments, "--manual", "--runtime-contract", str(root/'contract.json')]), \
                patch.object(command,"StoreRegistry"), patch.object(command,"RegistryUploadDbBackedRuntime"), \
                patch.object(command,"runtime_storage_admission"), patch.object(delegation,"standalone_history_worker",assert_parent_lease), redirect_stdout(StringIO()):
            assert command.main()==0
        assert admission_idle(runtime)["idle"] is True
        window = "history-fixture-window-001"
        state_path = runtime / STATE_FILENAME
        state = {"schema_version": SCHEMA_VERSION, "phase": "held", "window_id": window,
                 "window_kind": "maintenance_pause", "hold_confirmed": True}
        def set_state(value):
            state_path.write_text(json.dumps(value))
            state_path.chmod(0o600)
        set_state(state)
        def snapshot():
            return {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in runtime.iterdir()}
        before = snapshot()
        # Both ordinary manual parent and a direct internal worker fail before
        # source construction, candidate mkdir, capture or derived publication.
        for suffix in (["--manual"], ["--manual", "--worker"],
                       ["--manual", "--maintenance-window-id", "wrong-fixture-id"],
                       ["--manual", "--maintenance-window-id", "wrong-fixture-id", "--worker"],
                       ["--maintenance-window-id", window], ["--maintenance-window-id", window, "--worker"]):
            output = StringIO()
            with patch.object(sys, "argv", [*arguments, *suffix]), \
                    patch.object(command, "candidate_singleflight") as candidate, \
                    patch.object(command, "bounded_worker") as worker, \
                    patch.object(command, "StoreRegistry") as registry, redirect_stdout(output):
                command.main()
                assert json.loads(output.getvalue())["status"] == "skipped_maintenance"
                assert not candidate.called and not worker.called and not registry.called
            assert snapshot() == before
        allowed = [*arguments, "--manual", "--maintenance-window-id", window]
        def assert_exception_propagated(argv, seconds):
            assert argv[argv.index("--maintenance-window-id") + 1] == window
            assert "--manual" in argv and argv[-1] == "--worker"
            assert admission_idle(runtime)["idle"] is True
            return {"status": "fixture_only"}
        with patch.object(sys, "argv", allowed), patch.object(command, "admission", return_value="idle"), \
                patch.object(command, "bounded_worker", side_effect=assert_exception_propagated), redirect_stdout(StringIO()):
            assert command.main() == 0
        with patch.object(sys, "argv", [*allowed, "--worker"]), \
                patch.object(command, "StoreRegistry"), patch.object(command, "RegistryUploadDbBackedRuntime"), \
                patch.object(command, "LiveNativeAdapter"), patch.object(command, "HistoryStore"), \
                patch.object(command, "update_live_history", return_value={"status": "fixture_only"}) as update, \
                redirect_stdout(StringIO()):
            assert command.main() == 0 and update.called
        assert snapshot() == before
        for changed in ({"phase": "restoring"}, {"phase": "inactive"}, {"hold_confirmed": False},
                        {"window_kind": "snapshot"}):
            set_state({**state, **changed})
            output = StringIO()
            with patch.object(sys, "argv", allowed), patch.object(command, "bounded_worker") as worker, \
                    redirect_stdout(output):
                command.main()
                assert json.loads(output.getvalue())["status"] == "skipped_maintenance"
                assert not worker.called


def main():
    repo = Path(__file__).resolve().parents[1]
    contract_path = repo / 'artifacts/registry_upload_http_entrypoint/input/web_vitrina_history_runtime.json'
    contract = json.loads(contract_path.read_text())
    # Every file pinned by history must select this guard before release.
    checks = json.loads((repo / 'ci/checks.json').read_text())
    patterns = checks['groups']['web_vitrina_history_activation']['patterns']
    assert set(contract['formula_code_hashes']).issubset(patterns), 'history_formula_guard_not_selected'
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
    service_epoch = args[args.index('--formula-epoch') + 1]
    assert service_epoch == epoch, ('history_service_formula_epoch_drift', service_epoch, epoch)
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
        # The same pinned business date/clock reaches the fixed supervisor,
        # not a raw argv child. Actual FD/kernel behavior has Linux tests.
        from contextlib import contextmanager
        from packages.application import owned_history_worker as delegation
        observed={}
        @contextmanager
        def fixed_worker(*, runtime, config):
            def complete(captured, **kwargs):
                observed.update(now=captured,config=config,kwargs=kwargs)
                return {'status':'fixture_only'}
            yield SimpleNamespace(complete=complete)
        now=datetime(2026,10,3,20,0,tzinfo=timezone.utc)
        arguments[arguments.index('--candidate-root')+1]=str(fixture/'scheduled')
        with patch.object(sys,'argv',arguments), patch.object(command,'datetime') as clock, \
             patch.object(command,'deadline_seconds',side_effect=[10,7]), \
             patch.object(command,'runtime_storage_admission') as storage, \
             patch.object(command,'StoreRegistry'),patch.object(command,'RegistryUploadDbBackedRuntime'), \
             patch.object(delegation,'standalone_history_worker',fixed_worker),redirect_stdout(StringIO()):
            clock.now.return_value=now
            assert command.main()==0
        assert observed['now']==now and observed['kwargs']['source_range']==('2026-03-01','2026-10-04')
        assert observed['kwargs']['total_seconds']==7 and storage.called
        @contextmanager
        def failed_worker(**kwargs):
            from packages.application.owned_history_worker_capability import HistoryDelegationError
            raise HistoryDelegationError('fixture_child_failure')
            yield
        with patch.object(sys,'argv',arguments),patch.object(command,'deadline_seconds',return_value=7), \
             patch.object(command,'runtime_storage_admission'),patch.object(command,'StoreRegistry'), \
             patch.object(command,'RegistryUploadDbBackedRuntime'), \
             patch.object(delegation,'standalone_history_worker',failed_worker),redirect_stdout(StringIO()):
            assert command.main()==1
    check_maintenance_seam()
    print(json.dumps({'status': 'pass', 'fixture_only': True,
        'maintenance_parent_worker_gate_and_exact_manual_exception': True,
        'mount_reserve_formula_guards': True, 'missing_mount_before_writes': True,
        'current_business_date_pinned': True, 'existing_admission_shared_lock_hard_budget': True,
        'failed_child_nonzero_service_exit': True,
        'stable_reader_builder_path': str(root / 'history')}))


if __name__ == '__main__':
    main()
