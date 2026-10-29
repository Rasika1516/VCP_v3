"""Meter value generator.

Generates realistic OCPP MeterValues payloads for a charging session.
Supports both AC (single and three-phase) and DC charging profiles.

Charging model:
  - Ramp-up phase: power rises linearly from 0 to profile max over ramp_duration_s
  - Steady-state: holds at profile power (with ±2% jitter for realism)
  - Ramp-down: on stop request, power tapers over a short ramp-down period
  - Energy is accumulated each tick using the trapezoidal rule

For OCPP 1.6J, measurands are formatted as string values.
For OCPP 2.0.1, measurands use numeric float values.
"""
from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field
from typing import Any

from vcp_data_plane.config.schema import BehaviorConfig, EvseConnectorConfig, OcppVersion


@dataclass
class MeterSnapshot:
    """Current meter readings for one connector."""
    energy_wh: float = 0.0           # Total energy this session (Wh)
    power_w: float = 0.0             # Instantaneous power (W)
    current_a: float = 0.0           # Current (A)
    voltage_v: float = 0.0           # Voltage (V)
    soc_pct: float | None = None     # State of charge (optional, DC)
    temperature_c: float | None = None
    reactive_power_var: float = 0.0
    frequency_hz: float = 50.0
    # Timestamps
    session_start_ts: float = field(default_factory=time.monotonic)
    last_tick_ts: float = field(default_factory=time.monotonic)


class MeterGenerator:
    """Generates realistic meter value samples for one VCP connector."""

    def __init__(
        self,
        connector: EvseConnectorConfig,
        behavior: BehaviorConfig,
        dc: bool = False,
        ocpp_version: OcppVersion = OcppVersion.V16J,
    ) -> None:
        self._connector = connector
        self._behavior = behavior
        self._dc = dc
        self._ocpp_version = ocpp_version

        self._snapshot = MeterSnapshot()
        self._charging = False
        self._ramp_start_ts: float | None = None
        self._ramp_down_start_ts: float | None = None
        self._session_energy_start: float = 0.0

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def start_session(self) -> None:
        """Called when a charge session starts."""
        self._charging = True
        self._ramp_start_ts = time.monotonic()
        self._ramp_down_start_ts = None
        self._session_energy_start = self._snapshot.energy_wh
        self._snapshot.session_start_ts = time.monotonic()
        self._snapshot.last_tick_ts = time.monotonic()

    def stop_session(self) -> float:
        """Signal session end; returns total energy delivered this session (Wh)."""
        self._charging = False
        self._ramp_down_start_ts = time.monotonic()
        return self._snapshot.energy_wh - self._session_energy_start

    def is_charging(self) -> bool:
        return self._charging

    # ------------------------------------------------------------------
    # Tick (called each meter-value interval)
    # ------------------------------------------------------------------

    def tick(self) -> MeterSnapshot:
        """Advance the simulation by one tick and return current readings."""
        now = time.monotonic()
        dt = now - self._snapshot.last_tick_ts
        self._snapshot.last_tick_ts = now

        if self._charging:
            self._compute_charging_power(now)
        else:
            self._compute_idle_power(now)

        # Accumulate energy using trapezoidal rule
        self._snapshot.energy_wh += (self._snapshot.power_w * dt) / 3600.0

        # Derived values
        if self._snapshot.power_w > 0:
            if self._dc:
                # DC: voltage fixed, current varies
                self._snapshot.voltage_v = self._connector.max_voltage_v
                self._snapshot.current_a = self._snapshot.power_w / max(self._snapshot.voltage_v, 1)
            else:
                # AC: current varies, voltage nominally fixed ±1%
                self._snapshot.voltage_v = self._connector.max_voltage_v * random.uniform(0.99, 1.01)
                phases = self._connector.phase_count
                self._snapshot.current_a = self._snapshot.power_w / (
                    self._snapshot.voltage_v * phases * (math.sqrt(3) if phases == 3 else 1)
                )
                # Reactive power (power factor ~0.95-0.99 for a charger)
                pf = random.uniform(0.95, 0.99)
                self._snapshot.reactive_power_var = self._snapshot.power_w * math.tan(math.acos(pf))
        else:
            self._snapshot.current_a = 0.0
            self._snapshot.voltage_v = self._connector.max_voltage_v * random.uniform(0.99, 1.01) if not self._dc else 0.0
            self._snapshot.reactive_power_var = 0.0

        return self._snapshot

    def session_energy_wh(self) -> float:
        return self._snapshot.energy_wh - self._session_energy_start

    # ------------------------------------------------------------------
    # OCPP payload builders
    # ------------------------------------------------------------------

    def build_sampled_values_v16(self) -> list[dict[str, Any]]:
        """Build OCPP 1.6J SampledValue list from the current snapshot."""
        snap = self._snapshot
        measurands = self._behavior.meter_value.measurands
        svs = []

        def _sv(value: str, measurand: str, unit: str, phase: str | None = None) -> dict[str, Any]:
            d: dict[str, Any] = {
                "value": value,
                "context": "Sample.Periodic",
                "measurand": measurand,
                "unit": unit,
                "location": self._behavior.meter_value.location,
            }
            if phase:
                d["phase"] = phase
            return d

        for m in measurands:
            if m == "Energy.Active.Import.Register":
                svs.append(_sv(f"{snap.energy_wh:.1f}", m, "Wh"))
            elif m == "Power.Active.Import":
                svs.append(_sv(f"{snap.power_w:.1f}", m, "W"))
            elif m == "Current.Import":
                if self._connector.phase_count == 3:
                    for ph in ("L1", "L2", "L3"):
                        svs.append(_sv(f"{snap.current_a:.2f}", m, "A", ph))
                else:
                    svs.append(_sv(f"{snap.current_a:.2f}", m, "A"))
            elif m == "Voltage":
                if self._connector.phase_count == 3:
                    for ph in ("L1-N", "L2-N", "L3-N"):
                        svs.append(_sv(f"{snap.voltage_v:.1f}", m, "V", ph))
                else:
                    svs.append(_sv(f"{snap.voltage_v:.1f}", m, "V"))
            elif m == "Power.Reactive.Import":
                svs.append(_sv(f"{snap.reactive_power_var:.1f}", m, "var"))
            elif m == "Frequency":
                svs.append(_sv(f"{snap.frequency_hz:.2f}", m, "Hz"))
            elif m == "Temperature":
                if snap.temperature_c is not None:
                    svs.append(_sv(f"{snap.temperature_c:.1f}", m, "Celsius"))
            elif m == "SoC" and snap.soc_pct is not None:
                svs.append(_sv(f"{snap.soc_pct:.0f}", m, "Percent"))

        return svs

    def build_sampled_values_v201(self) -> list[dict[str, Any]]:
        """Build OCPP 2.0.1 SampledValue list from the current snapshot."""
        snap = self._snapshot
        svs = []

        def _uom(unit: str, multiplier: int = 0) -> dict[str, Any]:
            return {"unit": unit, "multiplier": multiplier}

        svs.append({
            "value": round(snap.energy_wh, 1),
            "context": "Sample.Periodic",
            "measurand": "Energy.Active.Import.Register",
            "unitOfMeasure": _uom("Wh"),
            "location": "Outlet",
        })
        svs.append({
            "value": round(snap.power_w, 1),
            "context": "Sample.Periodic",
            "measurand": "Power.Active.Import",
            "unitOfMeasure": _uom("W"),
            "location": "Outlet",
        })
        svs.append({
            "value": round(snap.current_a, 2),
            "context": "Sample.Periodic",
            "measurand": "Current.Import",
            "unitOfMeasure": _uom("A"),
            "location": "Outlet",
        })
        svs.append({
            "value": round(snap.voltage_v, 1),
            "context": "Sample.Periodic",
            "measurand": "Voltage",
            "unitOfMeasure": _uom("V"),
            "location": "Outlet",
        })
        if snap.soc_pct is not None:
            svs.append({
                "value": round(snap.soc_pct, 0),
                "measurand": "SoC",
                "unitOfMeasure": _uom("Percent"),
            })
        return svs

    # ------------------------------------------------------------------
    # Internal power computation
    # ------------------------------------------------------------------

    def _compute_charging_power(self, now: float) -> None:
        max_w = self._connector.max_power_kw * 1000.0
        ramp_dur = self._behavior.charge_ramp_duration_s

        if self._ramp_start_ts is not None:
            elapsed = now - self._ramp_start_ts
            if elapsed < ramp_dur:
                ramp_factor = elapsed / ramp_dur
                target_w = max_w * ramp_factor
            else:
                target_w = max_w
        else:
            target_w = max_w

        # Add ±2% realism jitter
        jitter = random.uniform(-0.02, 0.02)
        self._snapshot.power_w = max(0.0, target_w * (1 + jitter))

    def _compute_idle_power(self, now: float) -> None:
        if self._ramp_down_start_ts is None:
            self._snapshot.power_w = 0.0
            return
        elapsed = now - self._ramp_down_start_ts
        ramp_down_dur = min(self._behavior.charge_ramp_duration_s / 2, 15.0)
        if elapsed >= ramp_down_dur:
            self._snapshot.power_w = 0.0
            self._ramp_down_start_ts = None
        else:
            prev_power = self._snapshot.power_w
            factor = 1.0 - (elapsed / ramp_down_dur)
            self._snapshot.power_w = prev_power * factor
