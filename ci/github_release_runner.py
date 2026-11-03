#!/usr/bin/env python3
"""Trusted-main release entrypoint with held formula claim bootstrap."""
from pathlib import Path
import sys
import subprocess
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps import github_release_runner as runner
from ci.held_formula_deploy_claim import remote_shell


def prepare_deploy_ownership(operation):
    from apps import registry_upload_http_entrypoint_hosted_runtime as hosted
    with tempfile.TemporaryDirectory(prefix='wb-core-premerge-') as directory:
        runner.configure_ssh(Path(directory))
        target = hosted.load_hosted_runtime_target(hosted.resolve_target_file())
        command = remote_shell(app_dir=target.target_dir,
            runtime_dir=target.runtime_env['REGISTRY_UPLOAD_RUNTIME_DIR'],
            env_file=target.environment_file, operation=operation)
        subprocess.run(hosted._remote_shell_command(target, command), timeout=120, check=True)


def main():
    runner.prepare_deploy_ownership = prepare_deploy_ownership
    return runner.main()


if __name__ == '__main__':
    raise SystemExit(main())
