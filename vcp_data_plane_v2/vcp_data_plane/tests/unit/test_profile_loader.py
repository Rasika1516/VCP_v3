"""Unit tests for ChargerProfile loader and WorkerConfig loader."""
from __future__ import annotations

import textwrap
import pathlib
import pytest

from vcp_data_plane.config.loader import load_profile, load_profiles_dir, load_worker_config, ConfigError
from vcp_data_plane.config.schema import OcppVersion


# ---------------------------------------------------------------------------
# Charger profile YAMLs that match the actual schema
# ---------------------------------------------------------------------------

# ChargerProfile fields: profile_id, version, vendor, model, firmware,
# serial_number_prefix, ocpp_version, transport, security_profile,
# evses (list[EvseConfig]), capabilities, behavior, source, validated, tags
# EvseConfig fields: evse_id, connectors (list[EvseConnectorConfig]), dc, max_power_kw
# EvseConnectorConfig: connector_id, connector_type, max_power_kw, max_current_a,
#                       max_voltage_v, phase_count
# CapabilityConfig: remote_start, remote_stop, smart_charging, ...
# BehaviorConfig: heartbeat_interval_s, meter_value_interval_s,
#                 charge_ramp_duration_s, max_transaction_duration_s,
#                 idle_between_transactions_s (list), fault_injection_enabled, ...

AC_PROFILE_YAML = textwrap.dedent("""\
    profile_id: test_ac
    version: "1.0.0"
    vendor: TestVendor
    model: TestModel
    firmware: "1.0"
    ocpp_version: "1.6J"
    evses:
      - evse_id: 1
        dc: false
        connectors:
          - connector_id: 1
            connector_type: Type2
            max_power_kw: 7.4
            max_voltage_v: 230
            phase_count: 1
            max_current_a: 32
    capabilities:
      remote_start: true
      remote_stop: true
    behavior:
      heartbeat_interval_s: 300
      meter_value_interval_s: 60
      charge_ramp_duration_s: 120
      max_transaction_duration_s: 3600
      idle_between_transactions_s: [30.0, 120.0]
      fault_injection_enabled: true
""")

DC_PROFILE_YAML = textwrap.dedent("""\
    profile_id: test_dc
    version: "1.0.0"
    vendor: TestVendor
    model: FastDC
    firmware: "2.0"
    ocpp_version: "2.0.1"
    evses:
      - evse_id: 1
        dc: true
        connectors:
          - connector_id: 1
            connector_type: CCS2
            max_power_kw: 150.0
            max_voltage_v: 800
            phase_count: 0
            max_current_a: 375
    capabilities:
      remote_start: true
      remote_stop: true
      smart_charging: true
    behavior:
      heartbeat_interval_s: 300
      meter_value_interval_s: 30
      charge_ramp_duration_s: 60
      max_transaction_duration_s: 1800
      idle_between_transactions_s: [10.0, 30.0]
      fault_injection_enabled: false
""")

# WorkerConfig yaml using actual schema fields:
# fleet: FleetBlock with count, id_prefix, id_start, default_profile_id
# sharding: shard_index, shard_count
# boot_jitter_ms: [0, 5000] (list → tuple)
MINIMAL_WORKER_YAML = textwrap.dedent("""\
    worker_id: test-worker
    csms:
      url: ws://localhost:9000/ocpp
    redis:
      host: localhost
      port: 6379
      db: 0
    profile_dir: "{profile_dir}"
    fleet:
      count: 5
      id_prefix: "CP-"
      id_start: 1
      charge_point_id_prefix: "CP-"
      default_profile_id: test_ac
    vcps: []
    sharding:
      shard_count: 1
      shard_index: 0
    boot_jitter_ms: [0, 0]
    control_api:
      host: "0.0.0.0"
      port: 8080
      token: ""
      require_auth: false
    metrics:
      host: "0.0.0.0"
      port: 9090
""")


# ---------------------------------------------------------------------------
# Profile loading
# ---------------------------------------------------------------------------

def test_load_ac_profile(tmp_path: pathlib.Path) -> None:
    p = tmp_path / "test_ac.yaml"
    p.write_text(AC_PROFILE_YAML)
    profile = load_profile(str(p))
    assert profile.profile_id == "test_ac"
    assert profile.ocpp_version == OcppVersion.V16J
    assert len(profile.evses) == 1
    assert profile.evses[0].connectors[0].max_power_kw == 7.4


def test_load_dc_profile(tmp_path: pathlib.Path) -> None:
    p = tmp_path / "test_dc.yaml"
    p.write_text(DC_PROFILE_YAML)
    profile = load_profile(str(p))
    assert profile.profile_id == "test_dc"
    assert profile.ocpp_version == OcppVersion.V201
    assert profile.evses[0].dc is True
    assert profile.evses[0].connectors[0].connector_type == "CCS2"


def test_load_profile_primary_connector_properties(tmp_path: pathlib.Path) -> None:
    p = tmp_path / "test_ac.yaml"
    p.write_text(AC_PROFILE_YAML)
    profile = load_profile(str(p))
    conn = profile.primary_connector
    assert conn.max_power_kw == pytest.approx(7.4)
    assert conn.max_voltage_v == pytest.approx(230.0)
    assert conn.phase_count == 1


def test_load_profile_missing_profile_id(tmp_path: pathlib.Path) -> None:
    bad = textwrap.dedent("""\
        version: "1.0.0"
        vendor: BadVendor
        model: BadModel
    """)
    p = tmp_path / "bad.yaml"
    p.write_text(bad)
    with pytest.raises((ConfigError, Exception)):
        load_profile(str(p))


def test_load_profile_empty_version_raises(tmp_path: pathlib.Path) -> None:
    bad = textwrap.dedent("""\
        profile_id: test
        version: ""
        vendor: V
        model: M
    """)
    p = tmp_path / "bad.yaml"
    p.write_text(bad)
    with pytest.raises((ConfigError, Exception)):
        load_profile(str(p))


def test_load_profiles_dir(tmp_path: pathlib.Path) -> None:
    (tmp_path / "test_ac.yaml").write_text(AC_PROFILE_YAML)
    (tmp_path / "test_dc.yaml").write_text(DC_PROFILE_YAML)
    profiles = load_profiles_dir(str(tmp_path))
    assert "test_ac" in profiles
    assert "test_dc" in profiles
    assert len(profiles) == 2


def test_load_profiles_dir_empty_directory(tmp_path: pathlib.Path) -> None:
    profiles = load_profiles_dir(str(tmp_path))
    assert profiles == {}


def test_load_profiles_dir_skips_non_yaml(tmp_path: pathlib.Path) -> None:
    (tmp_path / "test_ac.yaml").write_text(AC_PROFILE_YAML)
    (tmp_path / "README.txt").write_text("not a profile")
    profiles = load_profiles_dir(str(tmp_path))
    assert len(profiles) == 1
    assert "test_ac" in profiles


def test_load_profiles_dir_skips_invalid_yaml(tmp_path: pathlib.Path) -> None:
    """Invalid profile is skipped with a warning; valid ones still load."""
    (tmp_path / "test_ac.yaml").write_text(AC_PROFILE_YAML)
    (tmp_path / "invalid.yaml").write_text("profile_id: bad\nversion: ''")  # empty version
    profiles = load_profiles_dir(str(tmp_path))
    assert "test_ac" in profiles
    # invalid profile is skipped, not raised
    assert "bad" not in profiles


# ---------------------------------------------------------------------------
# WorkerConfig loading
# ---------------------------------------------------------------------------

def test_load_worker_config_minimal(tmp_path: pathlib.Path) -> None:
    profile_dir = tmp_path / "profiles"
    profile_dir.mkdir()
    (profile_dir / "test_ac.yaml").write_text(AC_PROFILE_YAML)

    cfg_text = MINIMAL_WORKER_YAML.format(profile_dir=str(profile_dir))
    cfg_path = tmp_path / "worker.yaml"
    cfg_path.write_text(cfg_text)

    config = load_worker_config(str(cfg_path))
    assert config.worker_id == "test-worker"


def test_load_worker_config_fleet_expands_to_vcps(tmp_path: pathlib.Path) -> None:
    profile_dir = tmp_path / "profiles"
    profile_dir.mkdir()
    (profile_dir / "test_ac.yaml").write_text(AC_PROFILE_YAML)

    cfg_text = MINIMAL_WORKER_YAML.format(profile_dir=str(profile_dir))
    cfg_path = tmp_path / "worker.yaml"
    cfg_path.write_text(cfg_text)

    config = load_worker_config(str(cfg_path))
    # FleetBlock with count=5 should expand to 5 VCPs
    assert len(config.vcps) == 5


def test_load_worker_config_vcps_have_expected_ids(tmp_path: pathlib.Path) -> None:
    profile_dir = tmp_path / "profiles"
    profile_dir.mkdir()
    (profile_dir / "test_ac.yaml").write_text(AC_PROFILE_YAML)

    cfg_text = MINIMAL_WORKER_YAML.format(profile_dir=str(profile_dir))
    cfg_path = tmp_path / "worker.yaml"
    cfg_path.write_text(cfg_text)

    config = load_worker_config(str(cfg_path))
    vcp_ids = [v.vcp_id for v in config.vcps]
    # id_prefix="CP-", id_start=1, count=5 → CP-00001 … CP-00005
    assert "CP-00001" in vcp_ids
    assert "CP-00005" in vcp_ids


def test_load_worker_config_missing_file() -> None:
    with pytest.raises((ConfigError, Exception)):
        load_worker_config("/nonexistent/path/worker.yaml")


def test_load_worker_config_invalid_yaml(tmp_path: pathlib.Path) -> None:
    p = tmp_path / "bad.yaml"
    p.write_text("{{{{not valid yaml")
    with pytest.raises(Exception):
        load_worker_config(str(p))


# ---------------------------------------------------------------------------
# Environment variable substitution
# ---------------------------------------------------------------------------

def test_env_var_substitution(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_REDIS_HOST", "redis.internal")

    profile_dir = tmp_path / "profiles"
    profile_dir.mkdir()
    (profile_dir / "test_ac.yaml").write_text(AC_PROFILE_YAML)

    cfg_text = MINIMAL_WORKER_YAML.format(profile_dir=str(profile_dir))
    cfg_text = cfg_text.replace("host: localhost", "host: $TEST_REDIS_HOST")
    cfg_path = tmp_path / "worker.yaml"
    cfg_path.write_text(cfg_text)

    config = load_worker_config(str(cfg_path))
    assert config.redis.host == "redis.internal"


def test_env_var_absent_uses_literal_value(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When env var is absent, os.path.expandvars leaves it unchanged or uses fallback."""
    monkeypatch.delenv("ABSENT_VAR_XYZ", raising=False)

    profile_dir = tmp_path / "profiles"
    profile_dir.mkdir()
    (profile_dir / "test_ac.yaml").write_text(AC_PROFILE_YAML)

    cfg_text = MINIMAL_WORKER_YAML.format(profile_dir=str(profile_dir))
    # Without the env var, expandvars will keep the literal $ABSENT_VAR_XYZ
    cfg_text = cfg_text.replace("host: localhost", "host: redis-default")
    cfg_path = tmp_path / "worker.yaml"
    cfg_path.write_text(cfg_text)

    config = load_worker_config(str(cfg_path))
    assert config.redis.host == "redis-default"
