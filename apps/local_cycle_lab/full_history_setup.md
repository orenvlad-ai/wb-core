# Disposable arm64 Linux setup

Run from this checkout on an Apple Silicon Mac with Python 3.12+. These are the
commands used for the local acceptance VM. They require no Homebrew installation
or host `sudo`. Internet access is used to install the guest; application runs use
a private loopback-only network namespace. No host directories or SSH agent are
shared, and host SSH public keys are not imported.

Use a **new**, short state directory. Lima resolves symlinks before checking the
macOS Unix-socket path limit, so a symlink to a long checkout path does not work.
The original acceptance VM uses `/tmp/wbc-cycle-lab-stage4`; do not replace or
reconfigure that VM while it is in use. `/tmp` is disposable and may be cleared by
the OS. A short dedicated persistent path can be selected instead.

```sh
set -eu
export WBC_LAB_LIMA_HOME=/tmp/wbc-cycle-lab-fresh
export WBC_LAB_LIMA_INSTANCE=s4
export WBC_LAB_LIMA_BINARY="$PWD/output/stage4-vm/tool/bin/limactl"
test ! -e "$WBC_LAB_LIMA_HOME"
mkdir -m 700 "$WBC_LAB_LIMA_HOME"
mkdir -p output/stage4-vm/tool
curl -fL --retry 2 https://github.com/lima-vm/lima/releases/download/v2.3.0/lima-2.3.0-Darwin-arm64.tar.gz -o output/stage4-vm/lima.tar.gz
python3 - <<'PY'
from pathlib import Path
import hashlib, tarfile
p=Path('output/stage4-vm/lima.tar.gz')
assert hashlib.sha256(p.read_bytes()).hexdigest() == 'b7af444bd8f91d3c307477e562f2cf3bd7595d62b9429185e4fa841563578cfc'
with tarfile.open(p) as archive:
    archive.extractall('output/stage4-vm/tool', filter='data')
PY
cat > output/stage4-vm/fresh.yaml <<'YAML'
vmType: vz
arch: aarch64
cpus: 4
memory: 6GiB
disk: 40GiB
images:
- location: https://cloud-images.ubuntu.com/minimal/releases/noble/release-20261105/ubuntu-24.04-minimal-cloudimg-arm64.img
  arch: aarch64
  digest: sha256:3dc8370d923978d4b610fa7964d1b36ba040eefb43198809ca5bc8ac51777b18
mounts: []
ssh:
  loadDotSSHPubKeys: false
  forwardAgent: false
containerd:
  system: false
  user: false
portForwards:
- guestPortRange: [1, 65535]
  ignore: true
propagateProxyEnv: false
hostResolver:
  enabled: false
YAML
LIMA_HOME="$WBC_LAB_LIMA_HOME" "$WBC_LAB_LIMA_BINARY" start --name="$WBC_LAB_LIMA_INSTANCE" --tty=false output/stage4-vm/fresh.yaml
```

Wait for the `READY` result before package installation; cloud-init otherwise
still owns apt locks. The guest uses the pinned Ubuntu 24.04 arm64 image above.
Lima verifies its digest and may keep its ordinary image cache under the Mac
user's `Library/Caches/lima`; this does not install system software or alter
user-wide configuration.

```sh
python3 -m apps.local_cycle_lab.full_history_vm shell sudo bash -s <<'SH'
set -eu
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip e2fsprogs datefudge
useradd --create-home --shell /bin/bash wbc-lab
mkdir -p /opt/wbc-lab /var/lib/wbc-lab/runs /mnt/wb-core-extra100
chown wbc-lab:wbc-lab /var/lib/wbc-lab /var/lib/wbc-lab/runs
truncate -s 18G /var/lib/wbc-lab-history.ext4
mkfs.ext4 -q /var/lib/wbc-lab-history.ext4
mount -o loop,nosuid,nodev,noexec,noatime /var/lib/wbc-lab-history.ext4 /mnt/wb-core-extra100
mkdir -p /mnt/wb-core-extra100/local-history
chown wbc-lab:wbc-lab /mnt/wb-core-extra100/local-history
printf '%s\n' '/var/lib/wbc-lab-history.ext4 /mnt/wb-core-extra100 ext4 loop,nosuid,nodev,noexec,noatime 0 0' >> /etc/fstab
python3 -m venv /opt/wbc-lab-venv
/opt/wbc-lab-venv/bin/pip install apsw==3.54.1.0 openpyxl==3.1.5 pypdf==7.1.1 playwright==1.65.2
PLAYWRIGHT_BROWSERS_PATH=/opt/wbc-lab-browsers /opt/wbc-lab-venv/bin/playwright install --with-deps chromium
SH
python3 -m apps.local_cycle_lab.full_history_vm sync
```

The following installs the repository's actual heavy service definitions, with
guest paths and isolation drop-ins. It does not start services, install timers,
copy credentials, or replace admission probes. Missing production env files
remain missing. Every guarded unit must read `loaded`, `inactive`, `MainPID=0`.

```sh
python3 -m apps.local_cycle_lab.full_history_vm shell sudo /opt/wbc-lab-venv/bin/python - <<'PY'
from pathlib import Path
import subprocess, sys
sys.path.insert(0, '/opt/wbc-lab')
from apps.web_vitrina_finished_snapshot_build import HEAVY_UNITS
for name in HEAVY_UNITS:
    source=Path('/opt/wbc-lab/artifacts/registry_upload_http_entrypoint/systemd')/name
    text=source.read_text().replace('/opt/wb-core-runtime/app', '/opt/wbc-lab').replace('/opt/wb-core-runtime/state', '/var/lib/wbc-lab/inactive-services').replace('/usr/bin/python3', '/opt/wbc-lab-venv/bin/python')
    (Path('/etc/systemd/system')/name).write_text(text)
    dropin=Path('/etc/systemd/system')/(name+'.d')
    dropin.mkdir()
    (dropin/'local-lab-isolation.conf').write_text('[Service]\nUser=wbc-lab\nGroup=wbc-lab\nPrivateNetwork=yes\nNoNewPrivileges=yes\nCapabilityBoundingSet=\nProtectSystem=strict\nProtectHome=yes\n')
subprocess.run(['systemctl', 'daemon-reload'], check=True)
for name in HEAVY_UNITS:
    result=subprocess.check_output(['systemctl', 'show', name, '--property=LoadState,ActiveState,MainPID'], text=True)
    fields=dict(line.split('=', 1) for line in result.splitlines())
    assert fields == {'LoadState':'loaded', 'ActiveState':'inactive', 'MainPID':'0'}, (name, fields)
print('Native service admission prerequisites: PASS')
PY
python3 -m apps.local_cycle_lab.full_history_vm run apps.local_cycle_lab.full_history_probe > output/stage4-vm/fresh-guard-probe.json
```

The probe must succeed before a cycle. It checks non-root Linux/aarch64 execution,
no host mounts, only `lo`, actual `ENETUNREACH` for external TCP, inherited child
network isolation, native systemd admission, native storage admission and a real
headless Chromium click. The reviewed History hashes, formula epoch and reserve
of **10,871,635,968 bytes** remain unchanged. Only `candidate_root` and the actual
guest `findmnt` identity are substituted in the disposable contract.

Run the fresh synthetic scenario through the same isolated helper:

```sh
python3 -m apps.local_cycle_lab.full_history_vm sync
python3 -m apps.local_cycle_lab.full_history_vm run apps.local_cycle_lab.full_cycle --work /var/lib/wbc-lab/runs/control-1
```

`run` uses `systemd-run` with `User=wbc-lab`, `PrivateNetwork=yes`,
`NoNewPrivileges=yes`, empty capabilities, `ProtectSystem=strict`,
`ProtectHome=yes`, `PrivateTmp=yes`, and writes limited to `/var/lib/wbc-lab`
and the guest History mount. Both the local HTTP server/browser and native
History subprocess inherit that namespace. The full-cycle unit and its children use datefudge to advance realtime on the scenario day. Monotonic deadlines and filesystem timestamps use the real clock. A new work directory is required
for each trial. Never invoke the cycle directly through the unrestricted
provisioning `shell` command.

After the supervising task finishes, stop this exact VM explicitly:

```sh
LIMA_HOME="$WBC_LAB_LIMA_HOME" "$WBC_LAB_LIMA_BINARY" stop "$WBC_LAB_LIMA_INSTANCE"
```

Sources: [Lima 2.3.0 release](https://github.com/lima-vm/lima/releases/tag/v2.3.0),
[Lima configuration](https://lima-vm.io/docs/config/),
[Lima environment variables](https://lima-vm.io/docs/config/environment-variables/).
