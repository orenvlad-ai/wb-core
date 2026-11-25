"""macOS default-deny launcher. No import of WBC before sandbox entry."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]


def profile(state, port):
    def subpath(path):
        return "(subpath " + json.dumps(str(Path(path).resolve())) + ")"
    read = [ROOT / name for name in ("apps", "packages", "artifacts", "registry", "docs")]
    read += [Path(sys.prefix), Path(sys.base_prefix), Path("/System/Library"), Path("/System/Volumes/Preboot/Cryptexes/OS"),
             Path("/usr/lib"), Path("/usr/share/zoneinfo")]
    read += [state.root]
    # State writes are confined to disposable data, logs and stage artifacts.
    write = [state.root / name for name in ("runtime", "runs", "tmp", "home", "control")]
    return "\n".join([
        "(version 1)", "(deny default)",
        '(import "/System/Library/Sandbox/Profiles/dyld-support.sb")',
        "(allow file-read-metadata)",
        "(allow file-read* (literal " + json.dumps(str(ROOT)) + "))",
        "(allow sysctl-read)", "(allow signal (target self))", "(allow process-fork)",
        "(allow process-exec (literal " + json.dumps(str(Path(sys.executable).resolve())) + ")"
            + " (literal " + json.dumps(str(Path(sys.base_prefix) / "Resources/Python.app/Contents/MacOS/Python")) + "))",
        "(allow file-read* " + " ".join(map(subpath, read)) +
            ' (literal "/dev/urandom") (literal "/dev/random") (literal "/dev/null") (literal "/private/etc/localtime") (literal "/private/etc/apache2/mime.types"))',
        "(allow file-map-executable " + " ".join(map(subpath, read)) + ")",
        "(allow file-write* " + " ".join(map(subpath, write)) + ' (literal "/dev/null"))',
        f'(allow network-inbound (local tcp "localhost:{int(port)}"))',
        # No outbound exception: also deny localhost, UDP, AF_UNIX and Mach.
    ])


def clean_environment(state):
    return dict(HOME=str(state.root / "home"), TMPDIR=str(state.root / "tmp"),
        PATH="/usr/bin:/bin", TZ="UTC", LANG="en_US.UTF-8", PYTHONNOUSERSITE="1",
        PYTHONDONTWRITEBYTECODE="1", WB_API_TOKEN="lab-only", ONEC_STOCKS_ACCOUNT_ID="000000001",
        REGISTRY_UPLOAD_RUNTIME_DIR=str(state.runtime))


def launch(state, port, arguments, *, capture=False, lock_fd=None):
    if sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file():
        raise RuntimeError("Для этого launcher нужен macOS sandbox-exec; небезопасного fallback нет")
    command = ["/usr/bin/sandbox-exec", "-p", profile(state, port), sys.executable,
               "-s", "-B", "-m", "apps.local_cycle_lab.local", *arguments]
    environment = clean_environment(state)
    if lock_fd is not None:
        environment.update(WBC_LAB_LOCK_FD=str(lock_fd), WBC_LAB_SUPERVISOR_PID=str(os.getpid()))
    return subprocess.Popen(command, cwd=ROOT, env=environment, close_fds=True,
        pass_fds=() if lock_fd is None else (lock_fd,),
        start_new_session=True, stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None, text=True)


def probe(state, port):
    """Probe a readable secret and a listening local socket outside the sandbox."""
    import tempfile
    with tempfile.TemporaryDirectory(prefix="wbc-lab-isolation-", dir="/tmp") as directory:
        sentinel = Path(directory) / "dummy-secret"
        sentinel.write_text("synthetic isolation sentinel")
        with socket.socket() as listener, socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as unix:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            unix.bind(str(Path(directory) / "canary.sock"))
            unix.listen()
            with socket.create_connection(listener.getsockname(), timeout=1):
                pass  # Confirm the unsandboxed control succeeds, rather than testing a closed port.
            child = launch(state, port, ["_probe", "--state", str(state.root),
                "--sentinel", str(sentinel), "--probe-port", str(listener.getsockname()[1])], capture=True)
            stdout, stderr = child.communicate(timeout=15)
            if child.returncode:
                raise RuntimeError(f"Проверка песочницы не прошла ({child.returncode}): " + stderr[-4000:] + stdout[-1000:])
            return json.loads(stdout)
