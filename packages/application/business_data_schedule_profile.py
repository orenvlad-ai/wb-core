"""One dormant, fixed 24/7 business-data cadence; raw owner intent is separate."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat

from packages.application.business_data_write_barrier import _validate_identifier

PROFILE_ID = "business-data-cycle-3h-v1"
SCHEMA = "business_data_schedule_profile_v1"
SELECTOR_FILENAME = ".business-data-schedule-profile.json"
TRANSITIONS_DIRECTORY = ".business-data-schedule-transitions"
TRANSITION_SCHEMA = "business_data_schedule_transition_v1"
PLAN_SCHEMA = "business_data_schedule_target_plan_v1"
RECEIPT_SCHEMA = "business_data_schedule_target_restore_v1"
WAREHOUSE_TIMER = "wb-core-warehouse-functional-sync.timer"
RETIRED_TIMERS = (
    "wb-core-fbs-warehouse-registry.timer", "wb-core-wb-finance-daily.timer",
    "wb-core-wb-finance-weekly.timer", "wb-core-sheet-vitrina-refresh.timer",
    "wb-core-web-vitrina-finished-snapshot.timer",
    "wb-core-sheet-vitrina-closure-retry.timer",
)
CORE_TIMERS = (WAREHOUSE_TIMER,) + RETIRED_TIMERS
DROPIN_RELATIVE = WAREHOUSE_TIMER + ".d/50-business-data-cycle-profile.conf"
DROPIN_BYTES = ("[Timer]\nOnCalendar=\n"
                "OnCalendar=*-*-* 00,03,06,09,12,15,18,21:00:00 Asia/Yekaterinburg\n"
                "AccuracySec=1s\nRandomizedDelaySec=0\nPersistent=true\n"
                "Unit=wb-core-warehouse-functional-sync.service\n").encode()
PROFILE = {
    "profile_id": PROFILE_ID, "version": 1, "timezone": "Asia/Yekaterinburg",
    "slots": ["00:00", "03:00", "06:00", "09:00", "12:00", "15:00", "18:00", "21:00"],
    "fbs_collect_every_cycle": True, "fbs_max_age_seconds": 9 * 3600,
    "history_publisher": "rolling14", "owner_timer": WAREHOUSE_TIMER,
    "retired_timers": list(RETIRED_TIMERS),
    "dropin_sha256": hashlib.sha256(DROPIN_BYTES).hexdigest(),
}


def fingerprint(value) -> str:
    from packages.application.business_data_maintenance_pause import fingerprint as digest
    return digest(value)


def read_private(path: Path, *, optional=False) -> dict | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        if optional:
            return None
        raise
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 2 * 1024 * 1024:
            raise RuntimeError("schedule metadata is not a bounded private regular file")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(2 * 1024 * 1024 + 1)
        if len(data) > 2 * 1024 * 1024:
            raise RuntimeError("schedule metadata exceeds bound")
        value = json.loads(data)
        if not isinstance(value, dict):
            raise RuntimeError("schedule metadata is not an object")
        return value
    finally:
        os.close(fd)


def signed(value: dict, schema: str) -> dict:
    if value.get("schema_version") != schema or value.get("fingerprint") != fingerprint(
            {key: item for key, item in value.items() if key != "fingerprint"}):
        raise RuntimeError("schedule metadata schema/fingerprint mismatch")
    return value


def transition_path(runtime: Path, operation_id: str) -> Path:
    _validate_identifier(operation_id, label="operation_id")
    root = runtime / TRANSITIONS_DIRECTORY
    if root.is_symlink() or (root.exists() and (not root.is_dir() or root.stat().st_mode & 0o077)):
        raise RuntimeError("schedule transition directory is not private/safe")
    return root / (operation_id + ".json")


def load_transition(runtime: Path, operation_id: str) -> dict | None:
    value = read_private(transition_path(runtime, operation_id), optional=True)
    if value:
        signed(value, TRANSITION_SCHEMA)
        signed(value["plan"], PLAN_SCHEMA)
        if value.get("phase") not in {"prepared", "dropin_installed", "reloaded", "selector_selected",
                                      "timers_restoring", "target_verified", "committed", "released",
                                      "rolling_back", "rolled_back"}:
            raise RuntimeError("schedule transition phase is unknown")
        if value["plan"]["operation_id"] != operation_id or value["plan"]["runtime_dir"] != str(runtime.resolve()):
            raise RuntimeError("schedule transition identity differs")
        plan = value["plan"]
        if (plan.get("profile") != PROFILE or plan.get("profile_fingerprint") != fingerprint(PROFILE)
                or plan.get("baseline_fingerprint") != fingerprint(plan.get("baseline"))):
            raise RuntimeError("schedule transition fixed profile/baseline differs")
    return value


def assert_no_partial_transition(runtime: Path) -> None:
    root = runtime / TRANSITIONS_DIRECTORY
    if root.is_symlink() or (root.exists() and (not root.is_dir() or root.stat().st_mode & 0o077)):
        raise RuntimeError("schedule transition directory is unsafe")
    if not root.exists():
        return
    for index, path in enumerate(root.iterdir()):
        if index >= 128 or path.suffix != ".json":
            raise RuntimeError("schedule transition inventory is unsafe/excessive")
        value = load_transition(runtime, path.stem)
        if value["phase"] not in {"released", "rolled_back"}:
            raise RuntimeError("schedule transition requires exact same-operation recovery")
        if value["phase"] == "released" and not (runtime / SELECTOR_FILENAME).exists():
            raise RuntimeError("committed schedule profile selector is missing; legacy fallback refused")


def load_selector(runtime: Path) -> dict | None:
    value = read_private(runtime / SELECTOR_FILENAME, optional=True)
    if value is None:
        assert_no_partial_transition(runtime)
        return None  # Absence is exactly legacy, with no provisioning.
    signed(value, SCHEMA)
    if (value.get("profile") != PROFILE or value.get("profile_fingerprint") != fingerprint(PROFILE)
            or value.get("runtime_dir") != str(runtime.resolve())):
        raise RuntimeError("unknown schedule profile or target")
    record = load_transition(runtime, value["operation_id"])
    if (not record or record["phase"] not in {"committed", "released"}
            or record["plan"]["fingerprint"] != value["plan_fingerprint"]
            or any(value.get(key) != record["plan"].get(key)
                   for key in ("window_id", "baseline_fingerprint", "deployed_sha"))):
        raise RuntimeError("profile selection lacks committed exact transition proof")
    return value


def activation_dependencies() -> dict:
    """Reviewed code integration, not runtime availability or production timing.

    Actual operator-thread -> dated/current ready -> owned history child/native
    acknowledgement has independent Linux integration proof. Owner intent,
    infrastructure, storage admission and exact target selection remain separate
    mandatory checks. There is no argument, file flag or CLI bypass.
    """
    return {"ready": True, "contract": "business_data_cycle_activation_dependencies_v1",
            "blockers": []}


def required_phase_readiness(runtime: Path) -> dict:
    """Conservative initial-profile owner policy, not proof of runner wiring.

    These are existing managed owners, not invented source flags. Exact source
    ownership/eligibility still belongs to the separately reviewed integration.
    """
    from apps.business_data_maintenance import POLICY_FILENAME, POLICY_SCHEMA_VERSION
    policy = read_private(runtime / POLICY_FILENAME)
    if policy.get("schema_version") != POLICY_SCHEMA_VERSION:
        raise RuntimeError("required phase owner policy is unproven")
    owners = {key: (policy.get("processes") or {}).get(key, {}).get("desired")
              for key in ("warehouse_functional", "wb_finance_weekly", "vitrina_refresh")}
    blockers = ["required_phase_owner_disabled_or_unknown:" + key
                for key, desired in owners.items() if desired is not True]
    feature = raw_schedule_feature(runtime)
    if not feature["enabled"]:
        blockers.append("required_phase_owner_disabled_or_unknown:vitrina_raw_schedule_enable")
    return {"ready": not blockers, "required_raw_owner_intent": owners, "raw_schedule_feature": feature, "blockers": blockers}


def activation_readiness(runtime: Path) -> dict:
    wiring = activation_dependencies()
    phases = required_phase_readiness(runtime)
    from packages.application.business_data_heavy_admission import heavy_admission_status
    infrastructure = heavy_admission_status(runtime)
    blockers = wiring["blockers"] + phases["blockers"]
    if not infrastructure["ready"]:
        blockers.append("canonical_heavy_infrastructure_unproven")
    return {"ready": wiring["ready"] and phases["ready"] and infrastructure["ready"], "wiring": wiring,
            "required_phases": phases, "heavy_infrastructure": infrastructure, "blockers": blockers}


def effective_core(runtime: Path) -> dict[str, tuple[str, str]]:
    from apps.business_data_maintenance import POLICY_FILENAME, POLICY_SCHEMA_VERSION
    policy = read_private(runtime / POLICY_FILENAME)
    if policy.get("schema_version") != POLICY_SCHEMA_VERSION or not isinstance(policy.get("master_desired"), bool):
        raise RuntimeError("cycle master owner intent is unproven")
    if policy["master_desired"] and not required_phase_readiness(runtime)["ready"]:
        raise RuntimeError("mandatory cycle phase is disabled/unknown in raw owner intent")
    pair = ("enabled", "active") if policy["master_desired"] else ("disabled", "inactive")
    return {WAREHOUSE_TIMER: pair, **{unit: ("disabled", "inactive") for unit in RETIRED_TIMERS}}


def projected_schedule(runtime: Path, raw_feature_intent: dict) -> dict:
    """Settings seam: never substitute this projection into activity.feature_intent."""
    selector = load_selector(runtime)
    return {"raw_feature_intent": raw_feature_intent, "raw_intent_fingerprint": fingerprint(raw_feature_intent),
            "profile": PROFILE if selector else None,
            "effective_timer_states": effective_core(runtime) if selector else None}


def require_legacy_master(runtime: Path) -> None:
    assert_no_partial_transition(runtime)
    if load_selector(runtime) is not None:
        raise RuntimeError("legacy master maintenance does not support the selected cycle profile")


def prove_preset(systemd, *, unit_directory=Path("/etc/systemd/system")) -> None:
    import shlex
    state = systemd.unit_state(WAREHOUSE_TIMER)
    properties = state.get("properties") or {}
    path = unit_directory / DROPIN_RELATIVE
    if (path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o022 or path.read_bytes() != DROPIN_BYTES
            or path.parent.is_symlink() or path.parent.stat().st_mode & 0o022
            or {item.name for item in path.parent.glob("*.conf")} != {path.name}
            or shlex.split(properties.get("DropInPaths", "")) != [str(path)]):
        raise RuntimeError("exact fixed warehouse timer drop-in is unproven")
    fragment = Path(properties.get("FragmentPath", ""))
    expected = Path(__file__).resolve().parents[2] / "artifacts/registry_upload_http_entrypoint/systemd" / WAREHOUSE_TIMER
    if fragment.is_symlink() or not fragment.is_file() or fragment.read_bytes() != expected.read_bytes():
        raise RuntimeError("warehouse timer base fragment is not the reviewed preset base")


def raw_schedule_feature(runtime: Path) -> dict:
    """Existing raw JSON may be 0644; reject symlinks/writable/unbounded input."""
    from packages.application.sheet_vitrina_v1_auto_refresh import DEFAULT_STATE_FILENAME
    path = Path(runtime) / DEFAULT_STATE_FILENAME
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return {'enabled': True, 'bytes_fingerprint': None, 'source': 'legacy_defaults'}
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022 or info.st_size > 2 * 1024 * 1024:
            raise RuntimeError('raw schedule bytes are unsafe')
        raw = os.read(fd, 2 * 1024 * 1024 + 1)
        value = json.loads(raw)
    finally:
        os.close(fd)
    rows = value.get('schedules') if isinstance(value, dict) else None
    if not isinstance(rows, list) or any(not isinstance(row, dict) or not isinstance(row.get('enabled', True), bool) for row in rows):
        raise RuntimeError('raw vitrina feature enable intent is unproven')
    return {'enabled': any(row.get('enabled', True) for row in rows),
            'bytes_fingerprint': 'sha256:' + hashlib.sha256(raw).hexdigest(), 'source': 'saved_owner_intent'}


def project_settings(runtime: Path, payload: dict) -> dict:
    """Keep raw rows/policy for save and maintenance CAS; UI uses only effective rows."""
    selector = load_selector(Path(runtime))
    if selector is None:
        return payload
    from apps.business_data_maintenance import POLICY_FILENAME
    policy = read_private(Path(runtime) / POLICY_FILENAME)
    phases = required_phase_readiness(Path(runtime))
    enabled = policy.get('master_desired') is True and phases['ready']
    raw = {'schedules': payload.get('schedules', []), 'schedule_policy': payload.get('schedule_policy', {})}
    return {**payload, 'raw_feature_intent': raw, 'raw_intent_fingerprint': fingerprint(raw),
            'raw_schedule_feature': raw_schedule_feature(Path(runtime)),
            'profile': PROFILE, 'effective_schedule_policy': {'mode': 'fixed_cycle', 'interval_hours': 3, 'timezone': PROFILE['timezone']},
            'effective_schedules': [{'id': 'cycle_' + slot.replace(':', '_'), 'enabled': enabled,
                'local_time_hhmm': slot, 'timezone': PROFILE['timezone']} for slot in PROFILE['slots']],
            'systemd_timer': WAREHOUSE_TIMER, 'systemd_timer_name': WAREHOUSE_TIMER,
            'systemd_oncalendar': '*-*-* 00,03,06,09,12,15,18,21:00:00 Asia/Yekaterinburg',
            'schedule_editing_managed_by_cycle': True, 'can_edit_runtime': False,
            'schedule_mode_type': 'fixed_cycle', 'message': 'Полный цикл каждые 3 часа круглосуточно; время управляется общим профилем.',
            'run_now_supported': False, 'required_phase_readiness': phases,
            'source_owner_mapping': {'warehouse_functional': ['official_fbs_collect', 'warehouse_materialization'],
                'wb_finance_weekly': ['finance_daily_sources', 'finance_weekly_due_sources'],
                'vitrina_refresh': ['full_auto_daily', 'final_ready', 'rolling14']}}
