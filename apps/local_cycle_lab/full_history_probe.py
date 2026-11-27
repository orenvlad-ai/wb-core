"""Read back the real guest guards and browser before a disposable cycle."""
from pathlib import Path
import errno
import json
import socket
import subprocess
import sys

from apps.local_cycle_lab.full_history import isolation_evidence, prepare_history


def main():
    evidence = isolation_evidence()
    with socket.socket() as sock:
        sock.settimeout(1)
        failure = sock.connect_ex(('192.0.2.1', 443))
    assert failure == errno.ENETUNREACH, failure
    evidence['external_connect_errno'] = failure
    evidence['child_isolation'] = json.loads(subprocess.check_output([
        sys.executable, '-c', 'import json; from apps.local_cycle_lab.full_history import isolation_evidence; print(json.dumps(isolation_evidence()))'], text=True))
    assert evidence['child_isolation']['network_namespace'] == evidence['network_namespace']
    runtime = Path('/var/lib/wbc-lab/guard-probe')
    runtime.mkdir(exist_ok=True)
    config = prepare_history(runtime)
    from apps.web_vitrina_finished_snapshot_build import systemd_admission
    assert systemd_admission() == 'idle'
    evidence['history_contract'] = json.loads(config.runtime_contract.read_text())
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content('<button id="probe">Native guest browser</button>')
        page.locator('#probe').click()
        evidence['browser'] = browser.version
        browser.close()
    print(json.dumps(evidence, indent=2))


if __name__ == '__main__':
    main()
