"""Unit tests for MeterGenerator — power ramp curves and sampled values."""
from __future__ import annotations

import time
import pytest

from vcp_data_plane.behavior.meter import MeterGenerator
from vcp_data_plane.config.schema import BehaviorConfig, EvseConnectorConfig, OcppVersion


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_connector_ac(max_kw: float = 7.4, phase_count: int = 1) -> EvseConnectorConfig:
    return EvseConnectorConfig(
        connector_id=1,
        connector_type="Type2",
        max_power_kw=max_kw,
        max_voltage_v=230.0,
        phase_count=phase_count,
        max_current_a=32,
    )


def _make_connector_dc(max_kw: float = 150.0) -> EvseConnectorConfig:
    return EvseConnectorConfig(
        connector_id=1,
        connector_type="CCS2",
        max_power_kw=max_kw,
        max_voltage_v=800.0,
        phase_count=0,
        max_current_a=375,
    )


def _make_behavior(ramp_s: float = 60.0, mv_interval_s: int = 30) -> BehaviorConfig:
    return BehaviorConfig(
        heartbeat_interval_s=300,
        meter_value_interval_s=mv_interval_s,
        charge_ramp_duration_s=ramp_s,
        max_transaction_duration_s=0,
        idle_between_transactions_s=(30.0, 120.0),
        fault_injection_enabled=True,
    )


def _ac_gen(max_kw: float = 7.4, ramp_s: float = 60.0, phase_count: int = 1) -> MeterGenerator:
    return MeterGenerator(
        connector=_make_connector_ac(max_kw=max_kw, phase_count=phase_count),
        behavior=_make_behavior(ramp_s=ramp_s),
        dc=False,
        ocpp_version=OcppVersion.V16J,
    )


def _dc_gen(max_kw: float = 150.0, ramp_s: float = 30.0) -> MeterGenerator:
    return MeterGenerator(
        connector=_make_connector_dc(max_kw=max_kw),
        behavior=_make_behavior(ramp_s=ramp_s),
        dc=True,
        ocpp_version=OcppVersion.V201,
    )


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------

def test_is_charging_false_before_start() -> None:
    gen = _ac_gen()
    assert gen.is_charging() is False


def test_is_charging_true_after_start() -> None:
    gen = _ac_gen()
    gen.start_session()
    assert gen.is_charging() is True


def test_is_charging_false_after_stop() -> None:
    gen = _ac_gen()
    gen.start_session()
    gen.stop_session()
    assert gen.is_charging() is False


def test_stop_session_returns_energy_float() -> None:
    gen = _ac_gen(ramp_s=1.0)
    gen.start_session()
    time.sleep(0.05)
    gen.tick()
    energy = gen.stop_session()
    assert isinstance(energy, float)
    assert energy >= 0.0


# ---------------------------------------------------------------------------
# Tick — power and energy
# ---------------------------------------------------------------------------

def test_tick_returns_meter_snapshot() -> None:
    gen = _ac_gen()
    gen.start_session()
    snap = gen.tick()
    assert snap.power_w >= 0.0


def test_tick_idle_power_is_zero_before_session() -> None:
    gen = _ac_gen()
    snap = gen.tick()
    assert snap.power_w == 0.0


def test_tick_accumulates_energy() -> None:
    gen = _ac_gen(ramp_s=1.0)
    gen.start_session()
    time.sleep(0.1)
    snap1 = gen.tick()
    time.sleep(0.1)
    snap2 = gen.tick()
    assert snap2.energy_wh >= snap1.energy_wh


def test_power_does_not_exceed_max() -> None:
    gen = _ac_gen(max_kw=7.4, ramp_s=1.0)
    gen.start_session()
    time.sleep(0.05)
    for _ in range(10):
        snap = gen.tick()
        assert snap.power_w <= 7.4 * 1000 * 1.05  # within 5% jitter tolerance


def test_power_non_negative_during_session() -> None:
    gen = _ac_gen()
    gen.start_session()
    for _ in range(5):
        snap = gen.tick()
        assert snap.power_w >= 0.0


def test_session_energy_resets_between_sessions() -> None:
    gen = _ac_gen(ramp_s=1.0)
    gen.start_session()
    time.sleep(0.1)
    gen.tick()
    energy_s1 = gen.session_energy_wh()
    assert energy_s1 >= 0.0

    gen.stop_session()
    gen.start_session()
    energy_s2 = gen.session_energy_wh()
    # New session energy starts at zero
    assert energy_s2 == pytest.approx(0.0, abs=0.01)


# ---------------------------------------------------------------------------
# AC-specific: current and voltage derivation
# ---------------------------------------------------------------------------

def test_ac_tick_sets_voltage_near_nominal() -> None:
    gen = _ac_gen()
    gen.start_session()
    time.sleep(0.05)
    snap = gen.tick()
    # Voltage should be near nominal 230 V ± 1%
    assert snap.voltage_v == pytest.approx(230.0, rel=0.02)


def test_ac_tick_positive_current_during_charge() -> None:
    gen = _ac_gen(max_kw=7.4, ramp_s=1.0)
    gen.start_session()
    time.sleep(0.15)
    snap = gen.tick()
    if snap.power_w > 0:
        assert snap.current_a > 0.0


def test_ac_reactive_power_non_negative_during_charge() -> None:
    gen = _ac_gen(ramp_s=1.0)
    gen.start_session()
    time.sleep(0.15)
    snap = gen.tick()
    assert snap.reactive_power_var >= 0.0


# ---------------------------------------------------------------------------
# DC-specific
# ---------------------------------------------------------------------------

def test_dc_no_reactive_power() -> None:
    gen = _dc_gen()
    gen.start_session()
    time.sleep(0.05)
    snap = gen.tick()
    assert snap.reactive_power_var == 0.0


def test_dc_voltage_matches_connector_spec_during_charge() -> None:
    gen = _dc_gen(max_kw=150.0)
    gen.start_session()
    time.sleep(0.05)
    snap = gen.tick()
    if snap.power_w > 0:
        assert snap.voltage_v == pytest.approx(800.0, abs=1.0)


# ---------------------------------------------------------------------------
# OCPP 1.6J sampled values
# ---------------------------------------------------------------------------

def test_v16_sampled_values_returns_non_empty_list() -> None:
    gen = _ac_gen()
    gen.start_session()
    gen.tick()
    values = gen.build_sampled_values_v16()
    assert isinstance(values, list)
    assert len(values) >= 1


def test_v16_sampled_values_are_strings() -> None:
    gen = _ac_gen()
    gen.start_session()
    gen.tick()
    values = gen.build_sampled_values_v16()
    for sv in values:
        assert "measurand" in sv
        assert "value" in sv
        assert isinstance(sv["value"], str), "OCPP 1.6J requires string values"


def test_v16_energy_import_present() -> None:
    gen = _ac_gen()
    gen.start_session()
    gen.tick()
    values = gen.build_sampled_values_v16()
    measurands = [v["measurand"] for v in values]
    assert "Energy.Active.Import.Register" in measurands


def test_v16_power_active_import_present() -> None:
    gen = _ac_gen()
    gen.start_session()
    gen.tick()
    values = gen.build_sampled_values_v16()
    measurands = [v["measurand"] for v in values]
    assert "Power.Active.Import" in measurands


# ---------------------------------------------------------------------------
# OCPP 2.0.1 sampled values
# ---------------------------------------------------------------------------

def test_v201_sampled_values_returns_non_empty_list() -> None:
    gen = _dc_gen()
    gen.start_session()
    gen.tick()
    values = gen.build_sampled_values_v201()
    assert isinstance(values, list)
    assert len(values) >= 1


def test_v201_power_active_import_is_numeric() -> None:
    gen = _dc_gen()
    gen.start_session()
    gen.tick()
    values = gen.build_sampled_values_v201()
    pwr = next((v for v in values if v.get("measurand") == "Power.Active.Import"), None)
    assert pwr is not None
    assert isinstance(pwr["value"], (int, float))


def test_v201_power_has_unit_of_measure_w() -> None:
    gen = _dc_gen()
    gen.start_session()
    gen.tick()
    values = gen.build_sampled_values_v201()
    pwr = next((v for v in values if v.get("measurand") == "Power.Active.Import"), None)
    assert pwr is not None
    assert "unitOfMeasure" in pwr
    assert pwr["unitOfMeasure"]["unit"] == "W"


def test_v201_energy_active_import_present() -> None:
    gen = _dc_gen()
    gen.start_session()
    gen.tick()
    values = gen.build_sampled_values_v201()
    energy = next((v for v in values if v.get("measurand") == "Energy.Active.Import.Register"), None)
    assert energy is not None


def test_v201_dc_no_reactive_power_measurand() -> None:
    gen = _dc_gen()
    gen.start_session()
    gen.tick()
    values = gen.build_sampled_values_v201()
    reactive = [v for v in values if "Reactive" in v.get("measurand", "")]
    assert reactive == [], "DC chargers should not report reactive power"
