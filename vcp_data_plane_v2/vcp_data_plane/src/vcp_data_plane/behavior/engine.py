"""Behavior Engine: drives VCP charger behavior from a profile.

The BehaviorEngine orchestrates the charger lifecycle for one VCP:
  1. Connect → boot → heartbeat loop
  2. EV plug-in → authorize → start transaction → meter loop → stop → unplug
  3. Handle CSMS-initiated commands (remote start/stop, reset, availability)
  4. Support auto-transaction mode for load testing

The engine uses:
  - VcpStateMachine   to track state and enforce legal transitions
  - VcpTimerScheduler for recurring timers
  - MeterGenerator    for realistic meter values
  - OCPP adapter      for protocol-specific message construction

It does NOT know about WebSocket connections directly — the adapter handles that.
"""
from __future__ import annotations

import asyncio
import logging
import random
import uuid
from typing import Any, Awaitable, Callable

from vcp_data_plane.behavior.meter import MeterGenerator
from vcp_data_plane.behavior.timer import VcpTimerScheduler
from vcp_data_plane.config.schema import ChargerProfile, OcppVersion, VcpEntry
from vcp_data_plane.state.machine import StateTransitionError, VcpState, VcpStateMachine

logger = logging.getLogger(__name__)


class BehaviorEngine:
    """Profile-driven charger behavior engine for a single VCP.

    This class is intentionally free of OCPP adapter references in its
    interface — the adapter is injected at startup and called via stored
    callbacks.  This makes testing without a real connection straightforward.
    """

    def __init__(
        self,
        vcp_entry: VcpEntry,
        profile: ChargerProfile,
        state_machine: VcpStateMachine,
        adapter: Any,                        # V16Adapter | V201Adapter
        timer_scheduler: VcpTimerScheduler,
        metrics_hooks: Any | None = None,
        on_meter_value: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        is_connected: Callable[[], bool] | None = None,
    ) -> None:
        self._entry = vcp_entry
        # Optional observers / probes (contract v2 event publishing)
        self._on_meter_value = on_meter_value
        self._is_connected = is_connected
        self._profile = profile
        self._state = state_machine
        self._adapter = adapter
        self._timers = timer_scheduler
        self._hooks = metrics_hooks

        self._behavior = profile.behavior
        self._connector = profile.primary_connector
        self._is_dc = profile.primary_evse.dc

        # Session state
        self._id_tag: str = ""
        self._transaction_id: str | int | None = None
        self._remote_start_id: int = 0
        self._meter = MeterGenerator(
            connector=self._connector,
            behavior=self._behavior,
            dc=self._is_dc,
            ocpp_version=profile.effective_ocpp_version(vcp_entry.ocpp_version),
        )
        self._active_ocpp_version = profile.effective_ocpp_version(vcp_entry.ocpp_version)

        # Tracks whether this engine is running (boot loop active)
        self._running = False
        self._boot_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Engine lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the boot + heartbeat lifecycle.

        When a connection probe is wired, the BootNotification is deferred until
        the WebSocket is actually open (see ``on_connection_established``);
        otherwise the first boot attempt would fail and wait a full
        ``boot_retry_interval_s`` before retrying.
        """
        self._running = True
        if self._is_connected is None or self._is_connected():
            self.ensure_boot()

    def ensure_boot(self) -> None:
        """Start the boot lifecycle unless one is already in progress."""
        if not self._running:
            return
        if self._boot_task is not None and not self._boot_task.done():
            return
        self._boot_task = asyncio.create_task(self._boot_lifecycle(), name=f"{self._entry.vcp_id}:boot")

    async def on_connection_established(self) -> None:
        """Called by the runner each time the WebSocket (re)connects.

        A real charger re-sends BootNotification after it reboots or loses its
        backend session; the simulator does the same on every (re)connect.
        """
        self.ensure_boot()

    async def stop(self) -> None:
        """Gracefully stop the engine."""
        self._running = False
        self._timers.cancel_all()
        if self._boot_task and not self._boot_task.done():
            self._boot_task.cancel()
            try:
                await self._boot_task
            except asyncio.CancelledError:
                pass

    # ------------------------------------------------------------------
    # Boot lifecycle
    # ------------------------------------------------------------------

    async def _boot_lifecycle(self) -> None:
        """Send BootNotification and retry until Accepted, then start timers."""
        b = self._behavior
        attempt = 0
        while self._running and attempt <= b.boot_max_retries:
            try:
                if self._active_ocpp_version in (OcppVersion.V16, OcppVersion.V16J):
                    resp = await self._adapter.send_boot_notification()
                else:
                    resp = await self._adapter.send_boot_notification(reason="PowerUp")

                if resp.accepted:
                    logger.info("[%s] Boot accepted (interval=%ds)", self._entry.vcp_id, resp.interval)
                    await self._state.set_connection_state(VcpState.AVAILABLE)
                    await self._send_all_status_notifications()
                    self._start_heartbeat(resp.interval)
                    if self._behavior.transaction_auto_start:
                        asyncio.create_task(self._auto_transaction_loop(), name=f"{self._entry.vcp_id}:auto")
                    return

                logger.warning("[%s] Boot rejected — retry in %ds (attempt %d)",
                               self._entry.vcp_id, b.boot_retry_interval_s, attempt + 1)
                await asyncio.sleep(b.boot_retry_interval_s)
                attempt += 1

            except Exception as exc:  # noqa: BLE001
                logger.error("[%s] Boot error: %s", self._entry.vcp_id, exc)
                await asyncio.sleep(b.boot_retry_interval_s)
                attempt += 1

        logger.error("[%s] Boot failed after %d attempts", self._entry.vcp_id, attempt)

    async def _send_all_status_notifications(self) -> None:
        """Report initial connector status after boot."""
        for evse in self._profile.evses:
            for conn in evse.connectors:
                await self._send_status("Available", evse_id=evse.evse_id, connector_id=conn.connector_id)

    def _start_heartbeat(self, server_interval: int) -> None:
        interval = min(server_interval, self._behavior.heartbeat_interval_s)
        self._timers.start_heartbeat(interval, self._do_heartbeat)

    async def _do_heartbeat(self) -> None:
        try:
            await self._adapter.send_heartbeat()
            if self._hooks:
                self._hooks.on_heartbeat(self._entry.vcp_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[%s] Heartbeat error: %s", self._entry.vcp_id, exc)

    # ------------------------------------------------------------------
    # EV plug / unplug
    # ------------------------------------------------------------------

    async def plug_ev(self, id_tag: str = "", evse_id: int = 1, connector_id: int = 1) -> None:
        """Simulate EV plug-in event."""
        connector = self._state.get_connector(evse_id, connector_id)
        connector.is_ev_connected = True
        connector.id_tag = id_tag or f"TAG-{random.randint(10000, 99999)}"
        await self._state.transition(VcpState.PREPARING, "ev_plug_in", evse_id, connector_id)
        await self._send_status("Preparing", evse_id=evse_id, connector_id=connector_id)
        logger.info("[%s] EV plugged in (tag=%s)", self._entry.vcp_id, connector.id_tag)

    async def unplug_ev(self, evse_id: int = 1, connector_id: int = 1) -> None:
        """Simulate EV unplug."""
        connector = self._state.get_connector(evse_id, connector_id)
        connector.is_ev_connected = False
        # If charging, stop the transaction first
        if connector.state in (VcpState.CHARGING, VcpState.SUSPENDED):
            await self.stop_charge(reason="EVDisconnected", evse_id=evse_id, connector_id=connector_id)
        # stop_charge already returns the connector to Available; only transition
        # (and notify the CSMS) when the connector is not there yet.
        if connector.state != VcpState.AVAILABLE:
            await self._state.transition(VcpState.AVAILABLE, "ev_unplug", evse_id, connector_id)
            await self._send_status("Available", evse_id=evse_id, connector_id=connector_id)

    # ------------------------------------------------------------------
    # Authorization
    # ------------------------------------------------------------------

    async def authorize(self, id_tag: str, evse_id: int = 1, connector_id: int = 1) -> bool:
        """Request authorization from CSMS."""
        connector = self._state.get_connector(evse_id, connector_id)
        if self._active_ocpp_version in (OcppVersion.V16, OcppVersion.V16J):
            resp = await self._adapter.send_authorize(id_tag)
        else:
            resp = await self._adapter.send_authorize(id_tag)

        if resp.accepted:
            connector.id_tag = id_tag
            await self._state.transition(VcpState.AUTHORIZED, "authorized", evse_id, connector_id)
            logger.info("[%s] Authorized tag=%s", self._entry.vcp_id, id_tag)
            return True
        else:
            logger.info("[%s] Authorization rejected tag=%s status=%s", self._entry.vcp_id, id_tag, resp.status)
            await self._state.transition(VcpState.AVAILABLE, "auth_rejected", evse_id, connector_id)
            await self._send_status("Available", evse_id=evse_id, connector_id=connector_id)
            return False

    # ------------------------------------------------------------------
    # Charge start / stop
    # ------------------------------------------------------------------

    async def start_charge(self, evse_id: int = 1, connector_id: int = 1) -> bool:
        """Start a charging transaction."""
        connector = self._state.get_connector(evse_id, connector_id)
        if connector.state not in (VcpState.AUTHORIZED, VcpState.PREPARING):
            logger.warning("[%s] Cannot start charge in state %s", self._entry.vcp_id, connector.state.value)
            return False

        meter_start = int(self._meter._snapshot.energy_wh)
        id_tag = connector.id_tag or "ANONYMOUS"

        if self._active_ocpp_version in (OcppVersion.V16, OcppVersion.V16J):
            resp = await self._adapter.send_start_transaction(connector_id, id_tag, meter_start)
            if not resp.accepted:
                logger.warning("[%s] StartTransaction rejected status=%s", self._entry.vcp_id, resp.status)
                return False
            connector.transaction_id = resp.transaction_id
        else:
            # OCPP 2.0.1: transaction_id is allocated locally; confirmed via TransactionEvent response
            txn_id = str(uuid.uuid4())
            connector.transaction_id = txn_id
            await self._adapter.send_transaction_event_started(
                transaction_id=txn_id,
                evse_id=evse_id,
                connector_id=connector_id,
                trigger_reason="Authorized",
                id_token=id_tag,
            )

        connector.meter_start_wh = meter_start
        self._meter.start_session()
        await self._state.transition(VcpState.CHARGING, "charge_started", evse_id, connector_id)
        await self._send_status("Charging", evse_id=evse_id, connector_id=connector_id)

        # Start meter value timer
        self._timers.start_meter_values(
            self._behavior.meter_value_interval_s,
            lambda: self._do_meter_values(evse_id, connector_id),
        )

        # Start session timeout if configured
        if self._behavior.max_transaction_duration_s > 0:
            self._timers.start_session_timeout(
                self._behavior.max_transaction_duration_s,
                lambda: self.stop_charge(reason="Timeout", evse_id=evse_id, connector_id=connector_id),
            )

        logger.info("[%s] Charge started txn=%s", self._entry.vcp_id, connector.transaction_id)
        if self._hooks:
            self._hooks.on_transaction_started(self._entry.vcp_id)
        return True

    async def stop_charge(self, reason: str = "Local", evse_id: int = 1, connector_id: int = 1) -> None:
        """Stop a charging transaction."""
        connector = self._state.get_connector(evse_id, connector_id)
        if connector.state not in (VcpState.CHARGING, VcpState.SUSPENDED):
            logger.warning("[%s] Cannot stop charge in state %s", self._entry.vcp_id, connector.state.value)
            return

        self._timers.stop_meter_values()
        self._timers.cancel_session_timeout()

        energy_delivered = self._meter.stop_session()
        meter_stop = int(connector.meter_start_wh + energy_delivered)

        await self._state.transition(VcpState.FINISHING, "charge_stopping", evse_id, connector_id)
        await self._send_status("Finishing", evse_id=evse_id, connector_id=connector_id)

        if self._active_ocpp_version in (OcppVersion.V16, OcppVersion.V16J):
            await self._adapter.send_stop_transaction(
                transaction_id=int(connector.transaction_id or 0),
                meter_stop=meter_stop,
                reason=reason,
                id_tag=connector.id_tag or None,
            )
        else:
            await self._adapter.send_transaction_event_ended(
                transaction_id=str(connector.transaction_id or ""),
                evse_id=evse_id,
                connector_id=connector_id,
                trigger_reason="StopAuthorized",
                stopped_reason=reason,
                id_token=connector.id_tag or None,
            )

        connector.transaction_id = None
        connector.id_tag = ""
        await self._state.transition(VcpState.AVAILABLE, "charge_stopped", evse_id, connector_id)
        await self._send_status("Available", evse_id=evse_id, connector_id=connector_id)

        logger.info("[%s] Charge stopped reason=%s energy=%.2f Wh", self._entry.vcp_id, reason, energy_delivered)
        if self._hooks:
            self._hooks.on_transaction_stopped(self._entry.vcp_id)

    async def suspend_charge(self, evse_id: int = 1, connector_id: int = 1) -> None:
        """Suspend (pause) a charging transaction."""
        await self._state.transition(VcpState.SUSPENDED, "ev_suspended", evse_id, connector_id)
        await self._send_status("SuspendedEVSE", evse_id=evse_id, connector_id=connector_id)
        self._timers.stop_meter_values()

    async def resume_charge(self, evse_id: int = 1, connector_id: int = 1) -> None:
        """Resume a suspended transaction."""
        await self._state.transition(VcpState.CHARGING, "ev_resumed", evse_id, connector_id)
        await self._send_status("Charging", evse_id=evse_id, connector_id=connector_id)
        self._timers.start_meter_values(
            self._behavior.meter_value_interval_s,
            lambda: self._do_meter_values(evse_id, connector_id),
        )

    # ------------------------------------------------------------------
    # Meter values (timer callback)
    # ------------------------------------------------------------------

    async def _do_meter_values(self, evse_id: int = 1, connector_id: int = 1) -> None:
        connector = self._state.get_connector(evse_id, connector_id)
        if connector.state not in (VcpState.CHARGING, VcpState.SUSPENDED):
            return

        self._meter.tick()

        if self._active_ocpp_version in (OcppVersion.V16, OcppVersion.V16J):
            sampled = self._meter.build_sampled_values_v16()
            await self._adapter.send_meter_values(
                connector_id=connector_id,
                sampled_values=sampled,
                transaction_id=int(connector.transaction_id or 0) if connector.transaction_id else None,
            )
        else:
            sampled = self._meter.build_sampled_values_v201()
            await self._adapter.send_transaction_event_updated(
                transaction_id=str(connector.transaction_id or ""),
                evse_id=evse_id,
                connector_id=connector_id,
                trigger_reason="MeterValuePeriodic",
                meter_values=[{"timestamp": MeterGenerator._now_iso(), "sampledValue": sampled}]
                if sampled else None,
            )

        connector.meter_current_wh = connector.meter_start_wh + (
            self._meter._snapshot.energy_wh - self._meter._session_energy_start
        )
        if self._on_meter_value is not None:
            snap = self._meter._snapshot
            try:
                await self._on_meter_value({
                    "evse_id": evse_id,
                    "connector_id": connector_id,
                    "transaction_id": connector.transaction_id,
                    "energy_wh": round(snap.energy_wh, 2),
                    "session_energy_wh": round(snap.energy_wh - self._meter._session_energy_start, 2),
                    "power_w": round(snap.power_w, 1),
                    "current_a": round(snap.current_a, 2),
                    "voltage_v": round(snap.voltage_v, 1),
                    "soc_pct": snap.soc_pct,
                })
            except Exception as exc:  # noqa: BLE001
                logger.debug("[%s] meter observer failed: %s", self._entry.vcp_id, exc)

    # ------------------------------------------------------------------
    # CSMS-initiated command handlers (called by OCPP handlers)
    # ------------------------------------------------------------------

    async def on_remote_start(self, connector_id: int | None, id_tag: str) -> bool:
        evse_id = 1
        conn_id = connector_id or 1
        conn = self._state.get_connector(evse_id, conn_id)
        if conn.state == VcpState.AVAILABLE:
            await self.plug_ev(id_tag=id_tag, evse_id=evse_id, connector_id=conn_id)
            auth_ok = await self.authorize(id_tag, evse_id=evse_id, connector_id=conn_id)
            if auth_ok:
                return await self.start_charge(evse_id=evse_id, connector_id=conn_id)
        return False

    async def on_remote_start_v201(self, evse_id: int | None, connector_id: int | None, id_token: str, id_token_type: str, remote_start_id: int) -> str:
        self._remote_start_id = remote_start_id
        return "Accepted" if await self.on_remote_start(connector_id, id_token) else "Rejected"

    async def on_remote_stop(self, transaction_id: int | str) -> bool:
        for (eid, cid), conn in self._state._connectors.items():
            if conn.transaction_id == transaction_id:
                await self.stop_charge(reason="Remote", evse_id=eid, connector_id=cid)
                return True
        return False

    async def on_remote_stop_v201(self, transaction_id: str) -> str:
        return "Accepted" if await self.on_remote_stop(transaction_id) else "Rejected"

    async def on_reset(self, reset_type: str) -> None:
        logger.info("[%s] Reset requested type=%s", self._entry.vcp_id, reset_type)
        # Stop all active transactions
        for (eid, cid), conn in self._state._connectors.items():
            if conn.state == VcpState.CHARGING:
                await self.stop_charge(reason="HardReset", evse_id=eid, connector_id=cid)
        # Re-run boot lifecycle after a brief delay
        await asyncio.sleep(1)
        self.ensure_boot()

    async def on_unlock(self, connector_id: int) -> str:
        conn = self._state.get_connector(1, connector_id)
        if conn.state in (VcpState.AVAILABLE, VcpState.PREPARING):
            conn.is_locked = False
            return "Unlocked"
        return "UnlockFailed"

    async def on_get_config(self, keys: list[str]) -> dict[str, Any]:
        all_config = {
            "HeartbeatInterval": str(self._behavior.heartbeat_interval_s),
            "MeterValueSampleInterval": str(self._behavior.meter_value_interval_s),
            "ConnectorPhaseRotation": "Unknown",
            "NumberOfConnectors": str(len(self._profile.primary_evse.connectors)),
            "ChargePointVendor": self._profile.vendor,
            "ChargePointModel": self._profile.model,
        }
        if not keys:
            return {
                "known": [{"key": k, "readonly": False, "value": v} for k, v in all_config.items()],
                "unknown": [],
            }
        known = [{"key": k, "readonly": False, "value": all_config[k]} for k in keys if k in all_config]
        unknown = [k for k in keys if k not in all_config]
        return {"known": known, "unknown": unknown}

    async def on_change_config(self, key: str, value: str) -> str:
        if key == "HeartbeatInterval":
            try:
                new_interval = int(value)
                self._behavior.heartbeat_interval_s = new_interval
                self._timers.stop_heartbeat()
                self._timers.start_heartbeat(new_interval, self._do_heartbeat)
                return "Accepted"
            except ValueError:
                return "Rejected"
        if key == "MeterValueSampleInterval":
            try:
                self._behavior.meter_value_interval_s = int(value)
                return "Accepted"
            except ValueError:
                return "Rejected"
        return "NotSupported"

    async def on_trigger(self, requested_message: str, connector_id: int | None) -> str:
        conn_id = connector_id or 1
        evse_id = 1
        if requested_message == "Heartbeat":
            asyncio.create_task(self._do_heartbeat())
            return "Accepted"
        if requested_message == "StatusNotification":
            conn = self._state.get_connector(evse_id, conn_id)
            asyncio.create_task(self._send_status(conn.state.value, evse_id=evse_id, connector_id=conn_id))
            return "Accepted"
        if requested_message == "MeterValues":
            asyncio.create_task(self._do_meter_values(evse_id, conn_id))
            return "Accepted"
        if requested_message == "BootNotification":
            asyncio.create_task(self._boot_lifecycle())
            return "Accepted"
        return "NotImplemented"

    async def on_change_availability(self, connector_id: int, availability_type: str) -> str:
        conn = self._state.get_connector(1, connector_id)
        if availability_type == "Inoperative":
            if conn.state == VcpState.CHARGING:
                return "Scheduled"
            conn.is_unavailable = True
            await self._state.force_transition(VcpState.FAULTED, "set_inoperative", 1, connector_id)
            await self._send_status("Unavailable", connector_id=connector_id)
            return "Accepted"
        else:  # Operative
            conn.is_unavailable = False
            if conn.state == VcpState.FAULTED:
                await self._state.force_transition(VcpState.RECOVERING, "set_operative", 1, connector_id)
                await self._state.transition(VcpState.AVAILABLE, "recovered", 1, connector_id)
                await self._send_status("Available", connector_id=connector_id)
            return "Accepted"

    async def on_set_variables(self, variable_data: list[dict[str, Any]]) -> list[dict[str, Any]]:
        results = []
        for item in variable_data:
            results.append({
                "attributeStatus": "NotSupportedAttributeType",
                "component": item.get("component", {}),
                "variable": item.get("variable", {}),
            })
        return results

    async def on_get_variables(self, variable_data: list[dict[str, Any]]) -> list[dict[str, Any]]:
        results = []
        for item in variable_data:
            results.append({
                "attributeStatus": "NotFound",
                "component": item.get("component", {}),
                "variable": item.get("variable", {}),
            })
        return results

    # ------------------------------------------------------------------
    # Auto transaction loop (load test mode)
    # ------------------------------------------------------------------

    async def _auto_transaction_loop(self) -> None:
        """Continuously run plug-in → charge → unplug cycles."""
        b = self._behavior
        while self._running:
            try:
                lo, hi = b.idle_between_transactions_s
                await asyncio.sleep(random.uniform(lo, hi))
                id_tag = f"AUTO-{random.randint(1000, 9999)}"
                await self.plug_ev(id_tag=id_tag)
                auth_ok = await self.authorize(id_tag)
                if auth_ok:
                    started = await self.start_charge()
                    if started:
                        # Charge for a random duration within profile limits
                        max_dur = b.max_transaction_duration_s or 600
                        charge_dur = random.uniform(60, max_dur)
                        await asyncio.sleep(charge_dur)
                        await self.stop_charge(reason="Local")
                await self.unplug_ev()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                logger.debug("[%s] Auto transaction loop error: %s", self._entry.vcp_id, exc)
                await asyncio.sleep(5)

    # ------------------------------------------------------------------
    # Helper
    # ------------------------------------------------------------------

    async def _send_status(self, status: str, evse_id: int = 1, connector_id: int = 1) -> None:
        try:
            if self._active_ocpp_version in (OcppVersion.V16, OcppVersion.V16J):
                await self._adapter.send_status_notification(
                    connector_id=connector_id,
                    status=status,
                )
            else:
                await self._adapter.send_status_notification(
                    evse_id=evse_id,
                    connector_id=connector_id,
                    connector_status=status,
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[%s] StatusNotification error: %s", self._entry.vcp_id, exc)

    # Utility for meter.py reference (avoids import in lambda)
    @staticmethod
    def _now_iso() -> str:
        import datetime
        return datetime.datetime.now(datetime.timezone.utc).isoformat()
