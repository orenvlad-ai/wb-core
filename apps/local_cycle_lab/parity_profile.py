"""Select the real schedule profile against a dedicated guest systemd user manager."""

from pathlib import Path
import shutil
import subprocess

from apps.business_data_maintenance import POLICY_FILENAME, POLICY_SCHEMA_VERSION, SystemdClient
from packages.application import business_data_maintenance_pause as pause
from packages.application import business_data_schedule_profile as profile
from packages.application import business_data_schedule_transition as transition
from packages.application.business_data_heavy_admission import heavy_admitted
from .state import atomic_json

UNITS = Path("/home/wbc-lab/.config/systemd/user")
SOURCE = Path(__file__).resolve().parents[2] / "artifacts/registry_upload_http_entrypoint/systemd"


class GuestTimers(SystemdClient):
    def _run(self, args):
        return subprocess.run(["systemctl", "--user", *args], text=True, capture_output=True)


def select(entry):
    runtime = entry.runtime.runtime_dir
    manager = GuestTimers()
    # The guest account is reserved exclusively for this lab. Refuse existing files.
    UNITS.mkdir(parents=True, exist_ok=False)
    for timer in pause.TIMERS:
        shutil.copyfile(SOURCE / timer, UNITS / timer)
        # Only timer management is exercised. Scheduled units cannot contact external systems.
        (UNITS / timer.replace(".timer", ".service")).write_text(
            "[Unit]\nDescription=Local WBC timer target\n[Service]\nType=oneshot\nExecStart=/usr/bin/true\n"
        )
    manager._run(["daemon-reload"]).check_returncode()
    atomic_json(
        runtime / POLICY_FILENAME,
        dict(
            schema_version=POLICY_SCHEMA_VERSION,
            master_desired=True,
            processes={
                k: {"desired": True} for k in ("warehouse_functional", "wb_finance_weekly", "vitrina_refresh")
            },
        ),
    )
    # Persist the initial operator settings. On an empty store the legacy defaults
    # derive enabled_since_at from the current clock on each read.
    entry.handle_sheet_web_vitrina_auto_schedules_save_request(entry._raw_web_vitrina_auto_schedules())
    with heavy_admitted(runtime, operation="fixture_bootstrap"):
        pass
    options = dict(systemd=manager, activity_reader=entry.handle_business_data_maintenance_activity_request)
    window, operation = "lab-profile-window", "lab-select-profile"
    sha = (Path(__file__).resolve().parents[2] / ".wb-core-runtime-sha").read_text().strip()
    pause.pause(
        runtime,
        window_id=window,
        actor="local-lab",
        reason="Initialize isolated manual dispatch",
        wait_timeout_seconds=10,
        **options,
    )
    plan = transition.preview(
        runtime, operation_id=operation, window_id=window, deployed_sha=sha, unit_directory=UNITS, **options
    )
    receipt = transition.apply(
        runtime,
        reviewed_plan=plan,
        expected_fingerprint=plan["fingerprint"],
        deployed_sha=sha,
        actor="local-lab",
        reason="Select lab cycle profile",
        unit_directory=UNITS,
        **options,
    )
    if not receipt["exact_target_state_restored"] or not profile.activation_readiness(runtime)["ready"]:
        raise ValueError("Guest profile activation failed")
    return dict(
        profile_id=profile.PROFILE_ID,
        operation=operation,
        transition=profile.load_transition(runtime, operation)["phase"],
        timer=manager.unit_state(profile.WAREHOUSE_TIMER)["is_active"],
        deployed_sha=sha,
    )
