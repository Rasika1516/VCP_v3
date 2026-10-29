"""VCP Worker Runner — wires together all data plane components.

One Runner instance manages all VCPs assigned to this worker process.
Each VCP gets its own:
  - VcpStateMachine       (state tracking)
  - ConnectionManager     (WebSocket lifecycle)
  - OcppRouter            (message dispatch)
  - V16Adapter or V201Adapter  (protocol-specific messages)
  - V16Handlers or V201Handlers (inbound handler registration)
  - BehaviorEngine        (charger lifecycle driven by profile)
  - VcpTimerScheduler     (heartbeat, meter value, session timers)
  - FaultInjector         (fault injection)
  - ScenarioRunner        (built-in scenario execution)

The Runner also owns:
  - VcpRegistry           (state snapshot + Redis registration)
  - CommandListener       (Redis Streams command consumer)
  - TracePublisher        (Redis Streams trace output, vcp:trace:{vcp_id})
  - EventPublisher        (contract v2 events → vcp:events:worker:{worker_id})
  - WorkerMetrics         (Prometheus)
  - Worker API            (FastAPI)

Startup sequence:
  1. Load profiles
  2. Start Redis (registry + command listener + trace publisher)
  3. Build and start each VCP runtime
  4. Register worker in Redis
  5. Start command listener
  6. Start FastAPI worker API
  7. Block until stop signal
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

import redis.asyncio as aioredis

from vcp_data_plane.behavior.engine import BehaviorEngine
from vcp_data_plane.behavior.timer import VcpTimerScheduler
from vcp_data_plane.command.listener import CommandListener
from vcp_data_plane.config.loader import load_profiles_dir
from vcp_data_plane.config.schema import (
    ChargerProfile,
    CommandType,
    OcppVersion,
    VcpEntry,
    WorkerCommand,
    WorkerConfig,
)
from vcp_data_plane import contract
from vcp_data_plane.connection.manager import ConnectionManager
from vcp_data_plane.events.publisher import EventPublisher
from vcp_data_plane.fault.injector import FaultInjector, FaultType
from vcp_data_plane.metrics.collector import WorkerMetrics
from vcp_data_plane.ocpp.router import OcppRouter
from vcp_data_plane.ocpp.v16.adapter import V16Adapter
from vcp_data_plane.ocpp.v16.handlers import V16Handlers
from vcp_data_plane.ocpp.v201.adapter import V201Adapter
from vcp_data_plane.ocpp.v201.handlers import V201Handlers
from vcp_data_plane.scenario.scenarios import ScenarioRunner
from vcp_data_plane.state.machine import VcpState, VcpStateMachine
from vcp_data_plane.state.registry import VcpRegistry
from vcp_data_plane.trace.publisher import NullTracePublisher, TracePublisher

logger = logging.getLogger(__name__)


class VcpRuntime:
    """All components for a single VCP instance."""

    def __init__(
        self,
        entry: VcpEntry,
        profile: ChargerProfile,
        state_machine: VcpStateMachine,
        connection: ConnectionManager,
        router: OcppRouter,
        adapter: Any,  # V16Adapter | V201Adapter
        behavior: BehaviorEngine,
        fault: FaultInjector,
        scenario: ScenarioRunner,
        timers: VcpTimerScheduler,
    ) -> None:
        self.entry = entry
        self.profile = profile
        self.state = state_machine
        self.connection = connection
        self.router = router
        self.adapter = adapter
        self.behavior = behavior
        self.fault = fault
        self.scenario = scenario
        self.timers = timers


class WorkerRunner:
    """Wires and runs all VCP runtimes for one worker process."""

    def __init__(
        self,
        config: WorkerConfig,
        redis_client: aioredis.Redis | None = None,
        metrics_registry: Any = None,
    ) -> None:
        self._config = config
        # Prometheus registry (default: global REGISTRY served by /metrics).
        # Note: prometheus_client treats registry=None as "do not register".
        if metrics_registry is None:
            from prometheus_client import REGISTRY
            metrics_registry = REGISTRY
        self._metrics_registry = metrics_registry
        self._worker_id = config.worker_id
        # Optional shared Redis client (tests inject fakeredis; production
        # lets each component open its own connection)
        self._redis_client = redis_client
        self._runtimes: dict[str, VcpRuntime] = {}
        self._profiles: dict[str, ChargerProfile] = {}
        self._registry: VcpRegistry | None = None
        self._cmd_listener: CommandListener | None = None
        self._trace: TracePublisher | NullTracePublisher | None = None
        self._events: EventPublisher | None = None
        self._metrics: WorkerMetrics | None = None
        # In-flight command tasks + per-VCP serialisation locks
        self._cmd_tasks: set[asyncio.Task[Any]] = set()
        self._vcp_locks: dict[str, asyncio.Lock] = {}
        self._heartbeat_task: asyncio.Task[None] | None = None

    @property
    def worker_id(self) -> str:
        return self._worker_id

    @property
    def registry(self) -> VcpRegistry | None:
        return self._registry

    @property
    def runtimes(self) -> dict[str, "VcpRuntime"]:
        return self._runtimes

    async def start(self) -> None:
        """Start all components."""
        logger.info("Worker %s starting (%d VCPs)", self._worker_id, len(self._config.vcps))

        # 1. Load profiles
        self._profiles = load_profiles_dir(self._config.profile_dir)
        logger.info("Loaded %d profiles from %s", len(self._profiles), self._config.profile_dir)

        # 2. Init metrics
        self._metrics = WorkerMetrics(worker_id=self._worker_id, registry=self._metrics_registry)
        self._metrics.vcps_total.set(len(self._config.vcps))

        # 3. Init Redis-backed components
        redis_cfg = self._config.redis
        rc = self._redis_client
        self._registry = VcpRegistry(self._worker_id, redis_cfg, redis_client=rc)
        await self._registry.start()

        self._trace = TracePublisher(self._worker_id, redis_cfg, redis_client=rc)
        await self._trace.start()

        self._events = EventPublisher(self._worker_id, redis_cfg, redis_client=rc)
        await self._events.start()

        self._cmd_listener = CommandListener(self._worker_id, redis_cfg, redis_client=rc)
        self._register_command_handlers()

        # 4. Build VCP runtimes
        vcps = self._config.vcps
        logger.info("Building %d VCP runtimes", len(vcps))
        for vcp_entry in vcps:
            try:
                runtime = self._build_runtime(vcp_entry)
                self._runtimes[vcp_entry.vcp_id] = runtime
                self._registry.register(runtime.state)
            except Exception as exc:  # noqa: BLE001
                logger.error("Failed to build runtime for %s: %s", vcp_entry.vcp_id, exc)

        # 5. Register worker in Redis
        await self._registry.register_worker()

        # 6. Announce the worker + its VCP set to the control plane, then
        #    start consuming commands
        await self._publish_worker_status("online", include_vcps=True)
        await self._cmd_listener.start()
        if redis_cfg.worker_heartbeat_s > 0:
            self._heartbeat_task = asyncio.create_task(self._worker_heartbeat_loop(), name="worker-heartbeat")

        # 7. Start all VCP connections and behavior engines
        start_tasks = []
        for runtime in self._runtimes.values():
            start_tasks.append(asyncio.create_task(self._start_vcp_runtime(runtime)))

        if start_tasks:
            await asyncio.gather(*start_tasks, return_exceptions=True)

        logger.info("Worker %s running with %d VCPs", self._worker_id, len(self._runtimes))

    async def stop(self) -> None:
        """Gracefully stop all components."""
        logger.info("Worker %s stopping", self._worker_id)

        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
        for t in list(self._cmd_tasks):
            t.cancel()

        # Stop VCP runtimes
        stop_tasks = []
        for runtime in self._runtimes.values():
            stop_tasks.append(asyncio.create_task(self._stop_vcp_runtime(runtime)))
        if stop_tasks:
            await asyncio.gather(*stop_tasks, return_exceptions=True)

        # Stop command listener
        if self._cmd_listener:
            await self._cmd_listener.stop()

        # Deregister from Redis
        if self._registry:
            await self._registry.deregister_worker()
            await self._registry.stop()

        # Stop trace publisher
        if self._trace:
            await self._trace.stop()

        # Final worker_status, then close the event publisher
        if self._events:
            await self._publish_worker_status("offline")
            await self._events.stop()

        logger.info("Worker %s stopped", self._worker_id)

    async def dispatch_command(self, vcp_id: str, command: WorkerCommand) -> dict[str, Any]:
        """Dispatch a command directly to a VCP runtime (used by Worker API)."""
        runtime = self._runtimes.get(vcp_id)
        if runtime is None:
            return {"error": f"VCP {vcp_id!r} not found"}

        return await self._execute_and_report(runtime, command)

    # ------------------------------------------------------------------
    # VCP Runtime builder
    # ------------------------------------------------------------------

    def _build_runtime(self, entry: VcpEntry) -> VcpRuntime:
        profile = self._resolve_profile(entry)
        ocpp_version = profile.effective_ocpp_version(entry.ocpp_version)

        # State machine
        state_machine = VcpStateMachine(
            vcp_id=entry.vcp_id,
            connector_count=len(profile.primary_evse.connectors),
            evse_count=len(profile.evses),
        )

        # OCPP Router
        router = OcppRouter(vcp_id=entry.vcp_id, request_timeout_s=30.0)

        # Connection Manager
        connection = ConnectionManager(
            vcp_id=entry.vcp_id,
            charge_point_id=entry.charge_point_id,
            csms_config=self._config.csms,
            ocpp_version=ocpp_version,
            security_profile=profile.security_profile,
            state_machine=state_machine,
            on_message=router.handle_inbound,
            on_connected=self._make_on_connected(entry.vcp_id),
            on_disconnected=self._make_on_disconnected(entry.vcp_id),
            boot_jitter_ms=self._config.boot_jitter_ms,
        )

        # Wire router ↔ connection
        router.set_send_fn(connection.send)

        # Build protocol adapter
        timers = VcpTimerScheduler(vcp_id=entry.vcp_id)

        if ocpp_version in (OcppVersion.V16, OcppVersion.V16J):
            adapter = V16Adapter(
                vcp_id=entry.vcp_id,
                charge_point_id=entry.charge_point_id,
                router=router,
                profile=profile,
            )
        else:
            adapter = V201Adapter(
                vcp_id=entry.vcp_id,
                charge_point_id=entry.charge_point_id,
                router=router,
                profile=profile,
            )

        # Behavior engine
        behavior = BehaviorEngine(
            vcp_entry=entry,
            profile=profile,
            state_machine=state_machine,
            adapter=adapter,
            timer_scheduler=timers,
            metrics_hooks=self._metrics,
            on_meter_value=self._make_meter_observer(entry.vcp_id),
            is_connected=lambda: connection.is_connected,
        )

        # Fault injector
        fault = FaultInjector(vcp_id=entry.vcp_id, state_machine=state_machine)
        fault.bind(
            send_fn=connection.send,
            stop_fn=connection.stop,
            start_fn=connection.start,
        )

        # Scenario runner
        scenario = ScenarioRunner(
            vcp_id=entry.vcp_id,
            behavior_engine=behavior,
            fault_injector=fault,
            connection=connection,
        )

        # OCPP trace hook → vcp:trace:{vcp_id} + (optionally) ocpp_trace events
        router.set_trace_hook(self._make_trace_hook(entry.vcp_id))

        # Register OCPP handlers
        if ocpp_version in (OcppVersion.V16, OcppVersion.V16J):
            handlers = V16Handlers(
                vcp_id=entry.vcp_id,
                on_remote_start=behavior.on_remote_start,
                on_remote_stop=behavior.on_remote_stop,
                on_reset=behavior.on_reset,
                on_unlock=behavior.on_unlock,
                on_get_config=behavior.on_get_config,
                on_change_config=behavior.on_change_config,
                on_trigger=behavior.on_trigger,
                on_change_availability=behavior.on_change_availability,
            )
            handlers.register_all(router)
        else:
            handlers = V201Handlers(
                vcp_id=entry.vcp_id,
                on_remote_start=behavior.on_remote_start_v201,
                on_remote_stop=behavior.on_remote_stop_v201,
                on_reset=behavior.on_reset,
                on_set_variables=behavior.on_set_variables,
                on_get_variables=behavior.on_get_variables,
                on_change_availability=lambda evse, conn, status: behavior.on_change_availability(conn or 1, status),
                on_trigger=behavior.on_trigger,
                on_unlock=lambda evse, conn: behavior.on_unlock(conn or 1),
            )
            handlers.register_all(router)

        # State change listener: publish snapshot to Redis + state_change event
        async def _on_state_change(event: Any) -> None:
            await self._publish_state(entry.vcp_id, reason=getattr(event, "reason", ""))
            if self._metrics:
                self._metrics.update_state_distribution(self._registry.summary())

        state_machine.add_listener(_on_state_change)
        state_machine.add_connection_listener(_on_state_change)

        return VcpRuntime(
            entry=entry,
            profile=profile,
            state_machine=state_machine,
            connection=connection,
            router=router,
            adapter=adapter,
            behavior=behavior,
            fault=fault,
            scenario=scenario,
            timers=timers,
        )

    def _resolve_profile(self, entry: VcpEntry) -> ChargerProfile:
        """Look up the charger profile for a VCP entry."""
        profile = self._profiles.get(entry.profile_id)
        if profile is None:
            raise ValueError(
                f"Profile {entry.profile_id!r} not found for VCP {entry.vcp_id}. "
                f"Available: {list(self._profiles)}"
            )
        return profile

    # ------------------------------------------------------------------
    # Start / stop individual VCP runtime
    # ------------------------------------------------------------------

    async def _start_vcp_runtime(self, runtime: VcpRuntime) -> None:
        try:
            await runtime.connection.start()
            await runtime.behavior.start()
            if self._metrics:
                self._metrics.vcps_connected.inc()
        except Exception as exc:  # noqa: BLE001
            logger.error("[%s] Failed to start VCP runtime: %s", runtime.entry.vcp_id, exc)

    async def _stop_vcp_runtime(self, runtime: VcpRuntime) -> None:
        try:
            await runtime.behavior.stop()
            await runtime.connection.stop()
        except Exception as exc:  # noqa: BLE001
            logger.error("[%s] Error stopping VCP runtime: %s", runtime.entry.vcp_id, exc)

    # ------------------------------------------------------------------
    # Command dispatch
    # ------------------------------------------------------------------

    def _register_command_handlers(self) -> None:
        """Register all command types with the CommandListener."""
        if self._cmd_listener is None:
            return

        async def _handle(command: WorkerCommand) -> dict[str, Any]:
            # Never block the stream reader: each command runs in its own task
            # and reports its outcome as a command_result event.
            task = asyncio.create_task(self._handle_command(command), name=f"cmd:{command.command_id}")
            self._cmd_tasks.add(task)
            task.add_done_callback(self._cmd_tasks.discard)
            return {"ok": True, "status": "accepted"}

        for cmd_type in CommandType:
            self._cmd_listener.register(cmd_type, _handle)

        async def _reject(raw: dict[str, Any], reason: str) -> None:
            vcp_id = raw.get("vcp_id") or ""
            # Only report rejections for VCPs we own (or worker-wide commands)
            if vcp_id and vcp_id not in self._runtimes:
                return
            await self._publish_command_result(
                vcp_id, str(raw.get("command_id", "")), str(raw.get("command_type", "")),
                {"ok": False, "error": reason},
            )

        self._cmd_listener.on_reject(_reject)

    def _targets_for(self, command: WorkerCommand) -> list[VcpRuntime]:
        """Resolve which local VCPs a command applies to.

        * vcp_id set      → that VCP if owned here (broadcast copies for VCPs
                            owned by other workers are silently ignored)
        * group_id set    → VCPs of this worker in that group
        * neither         → every VCP of this worker
        """
        if command.vcp_id:
            rt = self._runtimes.get(command.vcp_id)
            return [rt] if rt else []
        group = command.group_id or command.payload.get("group_id")
        if group:
            return [rt for rt in self._runtimes.values() if rt.entry.group_id == group]
        return list(self._runtimes.values())

    async def _handle_command(self, command: WorkerCommand) -> None:
        if self._metrics:
            self._metrics.on_command_received(command.command_type.value)

        targets = self._targets_for(command)
        if not targets:
            if command.vcp_id and not command._via_broadcast:
                # Addressed to this worker but the VCP is unknown here
                await self._publish_command_result(
                    command.vcp_id, command.command_id, command.command_type.value,
                    {"ok": False, "error": f"VCP {command.vcp_id!r} not found on worker {self._worker_id}"},
                )
            return

        await asyncio.gather(
            *(self._execute_and_report(rt, command) for rt in targets),
            return_exceptions=True,
        )

    async def _execute_and_report(self, runtime: VcpRuntime, command: WorkerCommand) -> dict[str, Any]:
        vcp_id = runtime.entry.vcp_id
        lock = self._vcp_locks.setdefault(vcp_id, asyncio.Lock())
        async with lock:
            result = await self._execute_command(runtime, command)
        await self._publish_command_result(vcp_id, command.command_id, command.command_type.value, result)
        return result

    async def _execute_command(self, runtime: VcpRuntime, command: WorkerCommand) -> dict[str, Any]:
        """Execute one command against a specific VCP runtime."""
        ct = command.command_type
        p = command.payload
        vcp_id = runtime.entry.vcp_id

        logger.debug("[%s] Executing command: %s", vcp_id, ct.value)

        try:
            if ct == CommandType.CONNECT:
                await runtime.connection.start()
                return {"ok": True}

            if ct == CommandType.DISCONNECT:
                await runtime.connection.stop()
                return {"ok": True}

            if ct == CommandType.RECONNECT:
                await runtime.connection.stop()
                await asyncio.sleep(0.5)
                await runtime.connection.start()
                return {"ok": True}

            if ct == CommandType.PLUG_EV:
                await runtime.behavior.plug_ev(
                    id_tag=p.get("id_tag", ""),
                    evse_id=p.get("evse_id", 1),
                    connector_id=p.get("connector_id", 1),
                )
                return {"ok": True}

            if ct == CommandType.UNPLUG_EV:
                await runtime.behavior.unplug_ev(
                    evse_id=p.get("evse_id", 1),
                    connector_id=p.get("connector_id", 1),
                )
                return {"ok": True}

            if ct == CommandType.AUTHORIZE:
                ok = await runtime.behavior.authorize(
                    id_tag=p.get("id_tag", "TAG-MANUAL"),
                    evse_id=p.get("evse_id", 1),
                    connector_id=p.get("connector_id", 1),
                )
                return {"ok": ok}

            if ct == CommandType.START_CHARGE:
                ok = await runtime.behavior.start_charge(
                    evse_id=p.get("evse_id", 1),
                    connector_id=p.get("connector_id", 1),
                )
                return {"ok": ok}

            if ct == CommandType.STOP_CHARGE:
                await runtime.behavior.stop_charge(
                    reason=p.get("reason", "Local"),
                    evse_id=p.get("evse_id", 1),
                    connector_id=p.get("connector_id", 1),
                )
                return {"ok": True}

            if ct == CommandType.INJECT_FAULT:
                fault_type = FaultType(p.get("fault_type", "network_disconnect"))
                result = await runtime.fault.inject(fault_type, p)
                if self._metrics:
                    self._metrics.on_fault_injected(fault_type.value)
                return {"ok": result.injected, "message": result.message}

            if ct == CommandType.RECOVER:
                await runtime.fault.recover()
                return {"ok": True}

            if ct == CommandType.RUN_SCENARIO:
                return self._start_scenario(runtime, p)

            if ct == CommandType.STOP_SCENARIO:
                await runtime.scenario.stop_active()
                return {"ok": True}

            if ct == CommandType.SEND_METER_VALUES:
                await runtime.behavior._do_meter_values(
                    p.get("evse_id", 1), p.get("connector_id", 1)
                )
                return {"ok": True}

            if ct == CommandType.SEND_BOOT_NOTIFICATION:
                asyncio.create_task(runtime.behavior._boot_lifecycle())
                return {"ok": True}

            if ct == CommandType.GET_STATE:
                return {"ok": True, "state": runtime.state.snapshot()}

            # --- Operator actions simulated on the charger side ---------
            if ct == CommandType.RESET:
                return await self._local_reset(runtime, str(p.get("type", "Soft")))

            if ct == CommandType.CHANGE_STATE:
                desired = str(p.get("desired_state") or p.get("availability") or "Available")
                operative = desired.lower() in ("available", "operative")
                conn_ids = [int(p["connector_id"])] if p.get("connector_id") else [
                    c.connector_id for c in runtime.profile.primary_evse.connectors
                ]
                statuses = {}
                for cid in conn_ids:
                    statuses[cid] = await runtime.behavior.on_change_availability(
                        cid, "Operative" if operative else "Inoperative"
                    )
                ok = all(v in ("Accepted", "Scheduled") for v in statuses.values())
                return {"ok": ok, "desired_state": "Available" if operative else "Unavailable",
                        "status": statuses}

            if ct == CommandType.UNLOCK_CONNECTOR:
                evse_id = int(p.get("evse_id", 1))
                cid = int(p.get("connector_id", 1))
                conn = runtime.state.get_connector(evse_id, cid)
                if conn.state in (VcpState.CHARGING, VcpState.SUSPENDED):
                    await runtime.behavior.stop_charge(reason="UnlockCommand", evse_id=evse_id, connector_id=cid)
                if conn.state in (VcpState.PREPARING, VcpState.AUTHORIZED, VcpState.AVAILABLE) and conn.is_ev_connected:
                    await runtime.behavior.unplug_ev(evse_id=evse_id, connector_id=cid)
                status = await runtime.behavior.on_unlock(cid)
                return {"ok": status == "Unlocked", "status": status}

            if ct == CommandType.GET_CONFIGURATION:
                keys = p.get("keys") or []
                cfg = await runtime.behavior.on_get_config(list(keys))
                return {"ok": True, "configuration": cfg}

        except Exception as exc:  # noqa: BLE001
            logger.error("[%s] Command %s failed: %s", vcp_id, ct.value, exc)
            return {"ok": False, "error": str(exc)}

        return {"ok": False, "error": f"Unknown command: {ct.value}"}

    def _start_scenario(self, runtime: VcpRuntime, p: dict[str, Any]) -> dict[str, Any]:
        """Start a scenario from a run_scenario payload.

        Accepts both payload shapes:
          v2 (control plane): {scenario_id, execution_id?, <param>: <value>, ...}
          v1 (worker API):    {scenario, params: {...}}
        """
        requested = str(p.get("scenario_id") or p.get("scenario") or "")
        params = dict(p.get("params") or {})
        params.update({k: v for k, v in p.items() if k not in contract.RUN_SCENARIO_RESERVED_KEYS})
        canonical, params = contract.resolve_scenario(requested, params)
        if canonical not in runtime.scenario.KNOWN_SCENARIOS:
            return {"ok": False, "error": f"Unknown scenario {requested!r}"}

        execution_id = p.get("execution_id")
        vcp_id = runtime.entry.vcp_id

        async def _on_status(status: str, message: str) -> None:
            if self._events:
                await self._events.publish(contract.EVT_SCENARIO_STATUS, vcp_id, {
                    "execution_id": execution_id,
                    "scenario_id": canonical,
                    "requested_scenario_id": requested,
                    "status": status,
                    "message": message,
                })

        runtime.scenario.start_async(canonical, params, on_status=_on_status)
        return {"ok": True, "scenario": canonical, "status": "started", "execution_id": execution_id}

    async def _local_reset(self, runtime: VcpRuntime, reset_type: str) -> dict[str, Any]:
        """Simulate a charger reboot: end sessions, drop the link, reconnect and re-boot."""
        hard = reset_type.lower() == "hard"
        for (eid, cid), conn in list(runtime.state._connectors.items()):  # noqa: SLF001
            if conn.state in (VcpState.CHARGING, VcpState.SUSPENDED):
                await runtime.behavior.stop_charge(
                    reason="HardReset" if hard else "SoftReset", evse_id=eid, connector_id=cid,
                )
        await runtime.scenario.stop_active()
        runtime.timers.cancel_all()
        await runtime.connection.stop()
        await asyncio.sleep(3.0 if hard else 1.0)
        # Reconnect; on_connected → BehaviorEngine.on_connection_established → BootNotification
        await runtime.connection.start()
        return {"ok": True, "type": "Hard" if hard else "Soft", "status": "rebooting"}

    # ------------------------------------------------------------------
    # Event publishing (contract v2)
    # ------------------------------------------------------------------

    def _state_payload(self, runtime: VcpRuntime, reason: str = "") -> dict[str, Any]:
        snap = runtime.state.snapshot()
        conn_raw = snap["connection"]
        primary = runtime.state.primary.state.value
        actual = contract.normalise_actual_state(primary, conn_raw)
        if actual == "faulted" and runtime.state.primary.is_unavailable:
            actual = "unavailable"
        return {
            "actual_state": actual,
            "connection_state": contract.normalise_connection_state(conn_raw),
            "connector_state": snap["connectors"],
            "raw_connection_state": conn_raw,
            "raw_state": primary,
            "reason": reason,
            "charge_point_id": runtime.entry.charge_point_id,
            "profile_id": runtime.entry.profile_id,
            "group_id": runtime.entry.group_id,
            "ocpp_version": runtime.profile.effective_ocpp_version(runtime.entry.ocpp_version).value,
        }

    async def _publish_state(self, vcp_id: str, reason: str = "") -> None:
        if self._registry:
            await self._registry.publish_state(vcp_id)
        runtime = self._runtimes.get(vcp_id)
        if runtime is not None and self._events:
            await self._events.publish(contract.EVT_STATE_CHANGE, vcp_id, self._state_payload(runtime, reason))

    async def _publish_command_result(
        self, vcp_id: str, command_id: str, command_type: str, result: dict[str, Any],
    ) -> None:
        if not self._events:
            return
        ok = bool(result.get("ok", "error" not in result))
        await self._events.publish(contract.EVT_COMMAND_RESULT, vcp_id, {
            "command_id": command_id,
            "command_type": command_type,
            "ok": ok,
            "error": result.get("error") if not ok else None,
            "result": {k: v for k, v in result.items() if k not in ("ok", "error")},
        })

    def _vcp_descriptor(self, runtime: VcpRuntime) -> dict[str, Any]:
        return {
            "vcp_id": runtime.entry.vcp_id,
            "charge_point_id": runtime.entry.charge_point_id,
            "profile_id": runtime.entry.profile_id,
            "group_id": runtime.entry.group_id,
            "ocpp_version": runtime.profile.effective_ocpp_version(runtime.entry.ocpp_version).value,
        }

    async def _publish_worker_status(self, status: str, include_vcps: bool = False) -> None:
        if not self._events:
            return
        payload: dict[str, Any] = {
            "status": status,
            "contract_version": contract.CONTRACT_VERSION,
            "vcp_count": len(self._runtimes),
            "api_port": self._config.control_api.port,
            "state_distribution": self._registry.summary() if self._registry else {},
        }
        if include_vcps:
            payload["vcps"] = [self._vcp_descriptor(rt) for rt in self._runtimes.values()]
        await self._events.publish(contract.EVT_WORKER_STATUS, "", payload)

    async def _worker_heartbeat_loop(self) -> None:
        interval = self._config.redis.worker_heartbeat_s
        while True:
            try:
                await asyncio.sleep(interval)
                await self._publish_worker_status("heartbeat")
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001
                logger.debug("worker heartbeat failed: %s", exc)

    def _make_meter_observer(self, vcp_id: str):
        async def _observer(payload: dict[str, Any]) -> None:
            if self._events:
                await self._events.publish(contract.EVT_METER_VALUE, vcp_id, payload)
        return _observer

    def _make_trace_hook(self, vcp_id: str):
        forward = self._config.redis.forward_traces_to_events

        async def _hook(direction: str, action: str, message_id: str, payload: dict[str, Any]) -> None:
            if self._trace:
                await self._trace._publish(vcp_id, direction, action, message_id, payload)  # noqa: SLF001
            if self._metrics:
                if direction == "→":
                    self._metrics.on_message_sent(action)
                else:
                    self._metrics.on_message_received(action)
            if forward and self._events:
                await self._events.publish(contract.EVT_OCPP_TRACE, vcp_id, {
                    "dir": direction,
                    "action": action,
                    "message_id": message_id,
                    "payload": payload,
                })
        return _hook

    # ------------------------------------------------------------------
    # Connection event callbacks
    # ------------------------------------------------------------------

    def _make_on_connected(self, vcp_id: str):
        async def _on_connected(_: str) -> None:
            runtime = self._runtimes.get(vcp_id)
            if runtime:
                await runtime.state.set_connection_state(VcpState.RECOVERING, "ws_connected")
                # (Re)boot on every (re)connect — see BehaviorEngine.on_connection_established
                await runtime.behavior.on_connection_established()
            if self._metrics:
                self._metrics.connections_established.inc()
        return _on_connected

    def _make_on_disconnected(self, vcp_id: str):
        async def _on_disconnected(_: str) -> None:
            runtime = self._runtimes.get(vcp_id)
            if runtime:
                runtime.router.cancel_all_pending("disconnected")
                runtime.timers.cancel_all()
                await runtime.state.set_connection_state(VcpState.OFFLINE, "ws_disconnected")
            if self._metrics:
                self._metrics.vcps_connected.dec()
        return _on_disconnected
