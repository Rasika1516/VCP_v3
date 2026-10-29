"""Built-in Scenario Engine.

Provides named, reusable charging scenarios that can be triggered via the
command API or Redis Streams.  Scenarios are independent of charger profiles:
the same scenario runs against any profile and the CSMS behavior is what's
under test.

Built-in scenarios:
  boot_and_wait          — Boot → heartbeat (baseline connectivity check)
  plug_authorize_charge  — Full AC charge cycle: plug → authorize → charge → stop → unplug
  remote_start_charge    — Wait for CSMS RemoteStart, charge, then stop
  fault_and_recover      — Inject a connector fault, wait, then recover
  reconnect_storm        — Multiple rapid disconnect/reconnect cycles
  load_ramp              — Gradually ramp up charging power over time
  transaction_timeout    — Start a transaction then stop due to timeout
  csms_initiated_stop    — Start charge, wait for CSMS to send RemoteStop
  boot_storm             — Drop and re-open the connection (with jitter) so the
                           VCP re-boots; run fleet-wide to surge BootNotifications
  meter_value_flood      — High-rate MeterValues during a transaction (soak/load)

v1 control-plane aliases (resolved by contract.resolve_scenario):
  basic_charge_cycle → plug_authorize_charge   (duration_s → charge_duration_s)
  rapid_reconnect    → reconnect_storm
  remote_start_stop  → remote_start_charge     (duration_s → charge_duration_s)

Outcome reporting: start_async() accepts an ``on_status`` callback that is
called with (status, message) where status ∈ running | step | completed |
failed | cancelled.  A scenario that yields a line starting with
"Scenario failed" is reported as failed.

Each scenario is an async generator that yields status strings so callers
can track progress.
"""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, AsyncGenerator, Awaitable, Callable

from vcp_data_plane.contract import SCENARIO_IDS, resolve_scenario

# (status, message) → awaitable
ScenarioStatusFn = Callable[[str, str], Awaitable[None]]

logger = logging.getLogger(__name__)


class ScenarioError(Exception):
    """Raised when a scenario cannot proceed."""


class ScenarioRunner:
    """Executes built-in scenarios against a BehaviorEngine."""

    KNOWN_SCENARIOS: list[str] = list(SCENARIO_IDS)

    def __init__(
        self,
        vcp_id: str,
        behavior_engine: Any,
        fault_injector: Any,
        connection: Any | None = None,
    ) -> None:
        self._vcp_id = vcp_id
        self._engine = behavior_engine
        self._fault = fault_injector
        self._connection = connection   # ConnectionManager (boot_storm)
        self._active_scenario: str | None = None
        self._active_task: asyncio.Task[None] | None = None

    @property
    def active_scenario(self) -> str | None:
        return self._active_scenario

    async def run(self, scenario_name: str, params: dict[str, Any] | None = None) -> AsyncGenerator[str, None]:
        """Run a named scenario, yielding status strings as it progresses."""
        scenario_name, params = resolve_scenario(scenario_name, params)
        if scenario_name not in self.KNOWN_SCENARIOS:
            raise ScenarioError(f"Unknown scenario: {scenario_name!r}. Known: {self.KNOWN_SCENARIOS}")

        self._active_scenario = scenario_name
        logger.info("[%s] Starting scenario: %s params=%s", self._vcp_id, scenario_name, params)

        method_name = f"_scenario_{scenario_name}"
        method = getattr(self, method_name, None)
        if method is None:
            raise ScenarioError(f"No implementation for scenario {scenario_name!r}")

        try:
            async for status in method(params or {}):
                yield status
        finally:
            self._active_scenario = None

    def start_async(
        self,
        scenario_name: str,
        params: dict[str, Any] | None = None,
        on_status: ScenarioStatusFn | None = None,
    ) -> None:
        """Start a scenario in a background task (fire-and-forget).

        A scenario already running on this VCP is cancelled first.
        """
        async def _emit(status: str, message: str) -> None:
            if on_status is None:
                return
            try:
                await on_status(status, message)
            except Exception:  # noqa: BLE001
                logger.debug("[%s] scenario status callback failed", self._vcp_id)

        async def _runner() -> None:
            outcome, last = "completed", ""
            await _emit("running", f"Starting {scenario_name}")
            try:
                async for status in self.run(scenario_name, params):
                    last = status
                    logger.info("[%s] [%s] %s", self._vcp_id, scenario_name, status)
                    if status.startswith("Scenario failed"):
                        outcome = "failed"
                    elif not status.startswith("Scenario complete"):
                        await _emit("step", status)   # the final line is sent as the outcome
            except asyncio.CancelledError:
                outcome, last = "cancelled", "Scenario cancelled"
                await _emit(outcome, last)
                raise
            except Exception as exc:  # noqa: BLE001
                outcome, last = "failed", f"Scenario failed: {exc}"
                logger.error("[%s] Scenario %s failed: %s", self._vcp_id, scenario_name, exc)
            finally:
                if self._active_task is asyncio.current_task():
                    self._active_task = None
            await _emit(outcome, last)

        if self._active_task is not None and not self._active_task.done():
            self._active_task.cancel()
        self._active_task = asyncio.create_task(_runner(), name=f"{self._vcp_id}:{scenario_name}")

    async def stop_active(self) -> None:
        """Cancel the currently running scenario."""
        if self._active_task and not self._active_task.done():
            self._active_task.cancel()
            try:
                await self._active_task
            except asyncio.CancelledError:
                pass
        self._active_scenario = None

    # ------------------------------------------------------------------
    # Scenario implementations
    # ------------------------------------------------------------------

    async def _scenario_boot_and_wait(self, params: dict[str, Any]) -> AsyncGenerator[str, None]:
        wait_s = params.get("wait_s", 60)
        yield "Waiting for boot to complete"
        await asyncio.sleep(2)
        yield f"VCP booted — holding for {wait_s}s"
        await asyncio.sleep(wait_s)
        yield "Scenario complete: boot_and_wait"

    async def _scenario_plug_authorize_charge(self, params: dict[str, Any]) -> AsyncGenerator[str, None]:
        charge_duration_s = params.get("charge_duration_s", 120)
        id_tag = params.get("id_tag", f"TAG-{random.randint(10000, 99999)}")
        evse_id = params.get("evse_id", 1)
        connector_id = params.get("connector_id", 1)

        yield "Step 1: Plugging in EV"
        await self._engine.plug_ev(id_tag=id_tag, evse_id=evse_id, connector_id=connector_id)
        await asyncio.sleep(0.5)

        yield "Step 2: Authorizing"
        auth_ok = await self._engine.authorize(id_tag, evse_id=evse_id, connector_id=connector_id)
        if not auth_ok:
            yield "Scenario failed: Authorization rejected"
            return

        yield "Step 3: Starting charge"
        started = await self._engine.start_charge(evse_id=evse_id, connector_id=connector_id)
        if not started:
            yield "Scenario failed: StartTransaction rejected"
            return

        yield f"Step 4: Charging for {charge_duration_s}s"
        await asyncio.sleep(charge_duration_s)

        yield "Step 5: Stopping charge"
        await self._engine.stop_charge(reason="Local", evse_id=evse_id, connector_id=connector_id)
        await asyncio.sleep(0.5)

        yield "Step 6: Unplugging EV"
        await self._engine.unplug_ev(evse_id=evse_id, connector_id=connector_id)

        yield "Scenario complete: plug_authorize_charge"

    async def _scenario_remote_start_charge(self, params: dict[str, Any]) -> AsyncGenerator[str, None]:
        wait_timeout_s = params.get("wait_timeout_s", 30)
        charge_duration_s = params.get("charge_duration_s", 120)

        yield f"Waiting up to {wait_timeout_s}s for CSMS RemoteStart"
        # The actual remote start comes via on_remote_start in BehaviorEngine
        # We just wait for the state to change to Charging
        deadline = asyncio.get_event_loop().time() + wait_timeout_s
        while asyncio.get_event_loop().time() < deadline:
            from vcp_data_plane.state.machine import VcpState
            if self._engine._state.primary.state == VcpState.CHARGING:
                break
            await asyncio.sleep(1)
        else:
            yield "Scenario failed: CSMS did not send RemoteStart within timeout"
            return

        yield f"Charging for {charge_duration_s}s (CSMS-initiated)"
        await asyncio.sleep(charge_duration_s)

        yield "Stopping charge"
        await self._engine.stop_charge(reason="Remote")

        yield "Scenario complete: remote_start_charge"

    async def _scenario_fault_and_recover(self, params: dict[str, Any]) -> AsyncGenerator[str, None]:
        from vcp_data_plane.fault.injector import FaultType

        fault_type_str = params.get("fault_type", "connector_fault")
        fault_duration_s = params.get("fault_duration_s", 15)
        connector_id = params.get("connector_id", 1)

        try:
            fault_type = FaultType(fault_type_str)
        except ValueError:
            fault_type = FaultType.CONNECTOR_FAULT

        yield f"Step 1: Injecting {fault_type.value}"
        result = await self._fault.inject(
            fault_type,
            {"connector_id": connector_id, "error_code": params.get("error_code", "GroundFailure")},
        )
        if not result.injected:
            yield f"Scenario failed: {result.message}"
            return

        yield f"Step 2: Holding faulted state for {fault_duration_s}s"
        await asyncio.sleep(fault_duration_s)

        yield "Step 3: Recovering from fault"
        await self._fault.recover()
        await asyncio.sleep(1)

        yield "Scenario complete: fault_and_recover"

    async def _scenario_reconnect_storm(self, params: dict[str, Any]) -> AsyncGenerator[str, None]:
        from vcp_data_plane.fault.injector import FaultType

        cycles = params.get("cycles", 5)
        interval_s = params.get("interval_s", 2.0)

        yield f"Starting reconnect storm: {cycles} cycles @ {interval_s}s"
        result = await self._fault.inject(FaultType.RECONNECT_STORM, {"cycles": cycles, "interval_s": interval_s})
        if not result.injected:
            yield f"Scenario failed: {result.message}"
            return

        wait_s = cycles * interval_s * 3
        yield f"Waiting {wait_s:.0f}s for storm to complete"
        await asyncio.sleep(wait_s)

        yield "Scenario complete: reconnect_storm"

    async def _scenario_load_ramp(self, params: dict[str, Any]) -> AsyncGenerator[str, None]:
        id_tag = params.get("id_tag", f"TAG-{random.randint(10000, 99999)}")
        ramp_steps = params.get("ramp_steps", 5)
        step_duration_s = params.get("step_duration_s", 30)

        yield "Step 1: Plug EV and authorize"
        await self._engine.plug_ev(id_tag=id_tag)
        auth_ok = await self._engine.authorize(id_tag)
        if not auth_ok:
            yield "Scenario failed: Authorization rejected"
            return

        yield "Step 2: Start charge"
        started = await self._engine.start_charge()
        if not started:
            yield "Scenario failed: StartTransaction rejected"
            return

        yield f"Step 3: Ramping over {ramp_steps} steps × {step_duration_s}s each"
        for step in range(1, ramp_steps + 1):
            yield f"  Ramp step {step}/{ramp_steps}"
            await asyncio.sleep(step_duration_s)

        yield "Step 4: Stopping charge"
        await self._engine.stop_charge(reason="Local")
        await self._engine.unplug_ev()

        yield "Scenario complete: load_ramp"

    async def _scenario_transaction_timeout(self, params: dict[str, Any]) -> AsyncGenerator[str, None]:
        id_tag = params.get("id_tag", f"TAG-{random.randint(10000, 99999)}")
        timeout_s = params.get("timeout_s", 60)

        yield "Plug and start charge"
        await self._engine.plug_ev(id_tag=id_tag)
        await self._engine.authorize(id_tag)
        started = await self._engine.start_charge()
        if not started:
            yield "Scenario failed: could not start charge"
            return

        yield f"Waiting {timeout_s}s for timeout to trigger"
        await asyncio.sleep(timeout_s + 5)  # +5s grace

        yield "Scenario complete: transaction_timeout (stop triggered by timer)"

    async def _scenario_csms_initiated_stop(self, params: dict[str, Any]) -> AsyncGenerator[str, None]:
        id_tag = params.get("id_tag", f"TAG-{random.randint(10000, 99999)}")
        wait_s = params.get("wait_for_stop_s", 120)

        yield "Plug, authorize, start charge"
        await self._engine.plug_ev(id_tag=id_tag)
        await self._engine.authorize(id_tag)
        started = await self._engine.start_charge()
        if not started:
            yield "Scenario failed: could not start charge"
            return

        yield f"Charging — waiting up to {wait_s}s for CSMS to send RemoteStop"
        deadline = asyncio.get_event_loop().time() + wait_s
        from vcp_data_plane.state.machine import VcpState
        while asyncio.get_event_loop().time() < deadline:
            if self._engine._state.primary.state == VcpState.AVAILABLE:
                break
            await asyncio.sleep(2)
        else:
            yield f"Timeout: CSMS did not stop within {wait_s}s — stopping locally"
            await self._engine.stop_charge(reason="Local")

        await self._engine.unplug_ev()
        yield "Scenario complete: csms_initiated_stop"

    async def _scenario_boot_storm(self, params: dict[str, Any]) -> AsyncGenerator[str, None]:
        """Drop the WebSocket and reconnect after an optional random jitter.

        Run against a group or the whole fleet at once to produce a surge of
        reconnects + BootNotifications at the CSMS (mass-reboot simulation).
        """
        from vcp_data_plane.state.machine import VcpState

        jitter_ms = int(params.get("jitter_ms", 0))
        down_s = float(params.get("down_s", 1.0))
        timeout_s = float(params.get("boot_timeout_s", 60.0))
        if self._connection is None:
            yield "Scenario failed: no connection bound to scenario runner"
            return

        yield "Step 1: Dropping connection (simulated power cycle)"
        self._engine._timers.cancel_all()
        await self._connection.stop()
        delay = down_s + (random.uniform(0, jitter_ms) / 1000.0 if jitter_ms > 0 else 0.0)
        yield f"Step 2: Offline for {delay:.2f}s"
        await asyncio.sleep(delay)

        yield "Step 3: Reconnecting — VCP will send BootNotification"
        await self._connection.start()
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout_s
        state = self._engine._state
        while loop.time() < deadline:
            if state.connection_state == VcpState.AVAILABLE:
                break
            await asyncio.sleep(0.25)
        else:
            yield f"Scenario failed: boot not accepted within {timeout_s:.0f}s"
            return
        yield "Scenario complete: boot_storm"

    async def _scenario_meter_value_flood(self, params: dict[str, Any]) -> AsyncGenerator[str, None]:
        """Send MeterValues every ``interval_s`` for ``duration_s`` during a session."""
        from vcp_data_plane.state.machine import VcpState

        duration_s = float(params.get("duration_s", 300))
        interval_s = max(0.05, float(params.get("interval_s", 1.0)))
        id_tag = params.get("id_tag", f"TAG-{random.randint(10000, 99999)}")
        evse_id = params.get("evse_id", 1)
        connector_id = params.get("connector_id", 1)

        started_here = False
        conn = self._engine._state.get_connector(evse_id, connector_id)
        if conn.state != VcpState.CHARGING:
            yield "Step 1: Starting a transaction for the flood"
            await self._engine.plug_ev(id_tag=id_tag, evse_id=evse_id, connector_id=connector_id)
            if not await self._engine.authorize(id_tag, evse_id=evse_id, connector_id=connector_id):
                yield "Scenario failed: Authorization rejected"
                return
            if not await self._engine.start_charge(evse_id=evse_id, connector_id=connector_id):
                yield "Scenario failed: StartTransaction rejected"
                return
            started_here = True

        sent = 0
        yield f"Step 2: Flooding MeterValues every {interval_s}s for {duration_s:.0f}s"
        loop = asyncio.get_event_loop()
        deadline = loop.time() + duration_s
        while loop.time() < deadline:
            await self._engine._do_meter_values(evse_id, connector_id)
            sent += 1
            await asyncio.sleep(interval_s)

        if started_here:
            yield "Step 3: Stopping transaction"
            await self._engine.stop_charge(reason="Local", evse_id=evse_id, connector_id=connector_id)
            await self._engine.unplug_ev(evse_id=evse_id, connector_id=connector_id)
        yield f"Scenario complete: meter_value_flood ({sent} MeterValues sent)"
