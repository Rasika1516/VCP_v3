"""Fault Injector.

Provides controlled injection of abnormal conditions into VCP operation.
All fault types are explicitly scoped to test scenarios and are reported
as INJECTED so test results clearly identify simulator-induced failures
vs CSMS-originated failures.

Fault types:
  network_disconnect   — Close the WebSocket connection abruptly
  reconnect_storm      — Rapid repeated disconnect + reconnect cycles
  message_timeout      — Stop responding to CSMS CALLs for a duration
  malformed_payload    — Send a syntactically invalid OCPP message
  connector_fault      — Inject a Faulted connector state + StatusNotification
  power_outage         — Stop all traffic and stay offline for a duration
  slow_response        — Add artificial latency to all responses
  duplicate_messages   — Send a message twice consecutively

The injector does NOT modify the CSMS or physical hardware.  All effects
are limited to the VCP's WebSocket connection and OCPP message stream.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from dataclasses import dataclass
from enum import Enum
from typing import Any

from vcp_data_plane.state.machine import VcpState, VcpStateMachine

logger = logging.getLogger(__name__)


class FaultType(str, Enum):
    NETWORK_DISCONNECT = "network_disconnect"
    RECONNECT_STORM = "reconnect_storm"
    MESSAGE_TIMEOUT = "message_timeout"
    MALFORMED_PAYLOAD = "malformed_payload"
    CONNECTOR_FAULT = "connector_fault"
    POWER_OUTAGE = "power_outage"
    SLOW_RESPONSE = "slow_response"
    DUPLICATE_MESSAGES = "duplicate_messages"


@dataclass
class FaultResult:
    vcp_id: str
    fault_type: FaultType
    injected: bool
    message: str = ""


class FaultInjector:
    """Injects faults into a VCP's connection and OCPP message stream."""

    def __init__(self, vcp_id: str, state_machine: VcpStateMachine) -> None:
        self._vcp_id = vcp_id
        self._state = state_machine
        # Reference to the connection manager's send function
        self._send_fn: Any = None
        self._stop_fn: Any = None   # connection manager .stop()
        self._start_fn: Any = None  # connection manager .start()
        # Injected fault state
        self._message_timeout_active: bool = False
        self._slow_response_delay_s: float = 0.0
        self._timeout_task: asyncio.Task[None] | None = None
        self._storm_task: asyncio.Task[None] | None = None
        # Intercept flag: if True the router will drop inbound CALLs
        self.intercept_inbound: bool = False

    def bind(self, send_fn: Any, stop_fn: Any, start_fn: Any) -> None:
        """Bind to the connection manager functions."""
        self._send_fn = send_fn
        self._stop_fn = stop_fn
        self._start_fn = start_fn

    # ------------------------------------------------------------------
    # Fault injection entry point
    # ------------------------------------------------------------------

    async def inject(self, fault_type: FaultType, payload: dict[str, Any] | None = None) -> FaultResult:
        """Inject a fault. Returns a FaultResult describing what happened."""
        p = payload or {}
        logger.warning("[%s] INJECTING FAULT: %s params=%s", self._vcp_id, fault_type.value, p)

        try:
            if fault_type == FaultType.NETWORK_DISCONNECT:
                return await self._inject_disconnect()

            if fault_type == FaultType.RECONNECT_STORM:
                cycles = p.get("cycles", 5)
                interval_s = p.get("interval_s", 1.0)
                return await self._inject_reconnect_storm(cycles, interval_s)

            if fault_type == FaultType.MESSAGE_TIMEOUT:
                duration_s = p.get("duration_s", 30.0)
                return await self._inject_message_timeout(duration_s)

            if fault_type == FaultType.MALFORMED_PAYLOAD:
                return await self._inject_malformed_payload()

            if fault_type == FaultType.CONNECTOR_FAULT:
                connector_id = p.get("connector_id", 1)
                error_code = p.get("error_code", "GroundFailure")
                return await self._inject_connector_fault(connector_id, error_code)

            if fault_type == FaultType.POWER_OUTAGE:
                duration_s = p.get("duration_s", 10.0)
                return await self._inject_power_outage(duration_s)

            if fault_type == FaultType.SLOW_RESPONSE:
                delay_s = p.get("delay_s", 5.0)
                duration_s = p.get("duration_s", 60.0)
                return await self._inject_slow_response(delay_s, duration_s)

            if fault_type == FaultType.DUPLICATE_MESSAGES:
                return await self._inject_duplicate(p.get("message", ""))

        except Exception as exc:  # noqa: BLE001
            logger.error("[%s] Fault injection error: %s", self._vcp_id, exc)
            return FaultResult(self._vcp_id, fault_type, False, str(exc))

        return FaultResult(self._vcp_id, fault_type, False, f"Unknown fault type: {fault_type}")

    async def recover(self) -> None:
        """Clear injected fault state."""
        self._message_timeout_active = False
        self._slow_response_delay_s = 0.0
        self.intercept_inbound = False
        if self._timeout_task and not self._timeout_task.done():
            self._timeout_task.cancel()
        if self._storm_task and not self._storm_task.done():
            self._storm_task.cancel()

        # Transition to recovering if faulted
        if self._state.primary.state == VcpState.FAULTED:
            await self._state.transition(VcpState.RECOVERING, "fault_recovered")
            await self._state.transition(VcpState.AVAILABLE, "recovered")
        logger.info("[%s] Fault cleared", self._vcp_id)

    # ------------------------------------------------------------------
    # Slow response middleware (applied by router if active)
    # ------------------------------------------------------------------

    def get_response_delay(self) -> float:
        return self._slow_response_delay_s

    def is_message_timeout_active(self) -> bool:
        return self._message_timeout_active

    # ------------------------------------------------------------------
    # Individual fault implementations
    # ------------------------------------------------------------------

    async def _inject_disconnect(self) -> FaultResult:
        if self._stop_fn is None:
            return FaultResult(self._vcp_id, FaultType.NETWORK_DISCONNECT, False, "no stop_fn bound")
        await self._stop_fn()
        await self._state.set_connection_state(VcpState.OFFLINE, "fault_disconnect")
        return FaultResult(self._vcp_id, FaultType.NETWORK_DISCONNECT, True, "WebSocket closed abruptly")

    async def _inject_reconnect_storm(self, cycles: int, interval_s: float) -> FaultResult:
        if self._stop_fn is None or self._start_fn is None:
            return FaultResult(self._vcp_id, FaultType.RECONNECT_STORM, False, "no connect/stop bound")

        async def _storm() -> None:
            for i in range(cycles):
                logger.warning("[%s] Reconnect storm cycle %d/%d", self._vcp_id, i + 1, cycles)
                await self._stop_fn()
                await asyncio.sleep(interval_s * random.uniform(0.5, 1.5))
                await self._start_fn()
                await asyncio.sleep(interval_s)

        self._storm_task = asyncio.create_task(_storm(), name=f"{self._vcp_id}:storm")
        return FaultResult(self._vcp_id, FaultType.RECONNECT_STORM, True, f"{cycles} reconnect cycles started")

    async def _inject_message_timeout(self, duration_s: float) -> FaultResult:
        self._message_timeout_active = True
        self.intercept_inbound = True

        async def _clear_timeout() -> None:
            await asyncio.sleep(duration_s)
            self._message_timeout_active = False
            self.intercept_inbound = False
            logger.info("[%s] Message timeout fault cleared", self._vcp_id)

        self._timeout_task = asyncio.create_task(_clear_timeout(), name=f"{self._vcp_id}:timeout")
        return FaultResult(
            self._vcp_id, FaultType.MESSAGE_TIMEOUT, True,
            f"CSMS CALLs will be ignored for {duration_s:.0f}s",
        )

    async def _inject_malformed_payload(self) -> FaultResult:
        if self._send_fn is None:
            return FaultResult(self._vcp_id, FaultType.MALFORMED_PAYLOAD, False, "no send_fn bound")
        malformed = '{"broken JSON: missing closing brace'
        try:
            await self._send_fn(malformed)
        except Exception as exc:
            return FaultResult(self._vcp_id, FaultType.MALFORMED_PAYLOAD, False, str(exc))
        return FaultResult(self._vcp_id, FaultType.MALFORMED_PAYLOAD, True, "Malformed JSON sent to CSMS")

    async def _inject_connector_fault(self, connector_id: int, error_code: str) -> FaultResult:
        try:
            await self._state.force_transition(VcpState.FAULTED, f"injected:{error_code}", 1, connector_id)
        except Exception as exc:
            return FaultResult(self._vcp_id, FaultType.CONNECTOR_FAULT, False, str(exc))
        return FaultResult(
            self._vcp_id, FaultType.CONNECTOR_FAULT, True,
            f"Connector {connector_id} set to Faulted (error={error_code})",
        )

    async def _inject_power_outage(self, duration_s: float) -> FaultResult:
        if self._stop_fn is None or self._start_fn is None:
            return FaultResult(self._vcp_id, FaultType.POWER_OUTAGE, False, "no connect/stop bound")

        async def _outage() -> None:
            await self._stop_fn()
            await self._state.set_connection_state(VcpState.OFFLINE, "power_outage")
            logger.warning("[%s] Power outage: offline for %.0fs", self._vcp_id, duration_s)
            await asyncio.sleep(duration_s)
            logger.info("[%s] Power restored — reconnecting", self._vcp_id)
            await self._start_fn()

        asyncio.create_task(_outage(), name=f"{self._vcp_id}:outage")
        return FaultResult(self._vcp_id, FaultType.POWER_OUTAGE, True, f"Offline for {duration_s:.0f}s")

    async def _inject_slow_response(self, delay_s: float, duration_s: float) -> FaultResult:
        self._slow_response_delay_s = delay_s

        async def _clear_slow() -> None:
            await asyncio.sleep(duration_s)
            self._slow_response_delay_s = 0.0
            logger.info("[%s] Slow response fault cleared", self._vcp_id)

        asyncio.create_task(_clear_slow(), name=f"{self._vcp_id}:slow")
        return FaultResult(
            self._vcp_id, FaultType.SLOW_RESPONSE, True,
            f"{delay_s:.1f}s response delay for {duration_s:.0f}s",
        )

    async def _inject_duplicate(self, raw_message: str) -> FaultResult:
        if self._send_fn is None:
            return FaultResult(self._vcp_id, FaultType.DUPLICATE_MESSAGES, False, "no send_fn bound")
        if not raw_message:
            raw_message = json.dumps([2, "dup-test-id", "Heartbeat", {}])
        try:
            await self._send_fn(raw_message)
            await asyncio.sleep(0.05)
            await self._send_fn(raw_message)
        except Exception as exc:
            return FaultResult(self._vcp_id, FaultType.DUPLICATE_MESSAGES, False, str(exc))
        return FaultResult(self._vcp_id, FaultType.DUPLICATE_MESSAGES, True, "Message sent twice")
