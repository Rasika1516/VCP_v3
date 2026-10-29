"""VCP State Machine.

Implements the full charger state machine defined in the architecture:

   Available ──plug──▶ Preparing ──authorize──▶ Authorized ──start──▶ Charging
       ▲                                                                    │
       │                                            suspend/resume          │
       │                                    ┌──▶ Suspended ◀──────────────┤
       │                                    │                               │
       │                            Finishing ◀──────────────── stop/unplug┘
       │                                │
       └────────────────────────────────┘
   (Parallel: any state ──fault──▶ Faulted ──recover──▶ Recovering ──▶ original)
   (Any state ──disconnect──▶ Offline ──reconnect──▶ Recovering)

State machine is the authoritative runtime component: control commands and
scenario engine both REQUEST transitions; the machine decides legality.
Illegal transitions raise StateTransitionError rather than silently no-op.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Callable, Coroutine, Any

logger = logging.getLogger(__name__)


class VcpState(str, Enum):
    """All possible VCP operational states."""
    AVAILABLE = "Available"
    PREPARING = "Preparing"        # EV plugged in, not yet authorized
    AUTHORIZED = "Authorized"      # Authorization granted, awaiting charge start
    CHARGING = "Charging"          # Active energy transfer
    SUSPENDED = "Suspended"        # Charge paused (by EV or EVSE)
    FINISHING = "Finishing"        # Transaction ending (meter stop sent, waiting)
    FAULTED = "Faulted"           # Hardware/software fault detected
    RECOVERING = "Recovering"      # Recovering from fault or reconnect
    OFFLINE = "Offline"            # No WebSocket connection


class StateTransitionError(Exception):
    """Raised when a requested state transition is not legal."""
    def __init__(self, from_state: VcpState, to_state: VcpState) -> None:
        super().__init__(f"Illegal transition: {from_state.value} → {to_state.value}")
        self.from_state = from_state
        self.to_state = to_state


# Legal transition table: from_state → set of allowed to_states
_LEGAL_TRANSITIONS: dict[VcpState, set[VcpState]] = {
    VcpState.AVAILABLE: {
        VcpState.PREPARING,      # EV plug-in
        VcpState.FAULTED,
        VcpState.OFFLINE,
    },
    VcpState.PREPARING: {
        VcpState.AUTHORIZED,     # Authorization granted
        VcpState.AVAILABLE,      # EV unplug or auth failure
        VcpState.CHARGING,       # Auth + immediate start (some profiles)
        VcpState.FAULTED,
        VcpState.OFFLINE,
    },
    VcpState.AUTHORIZED: {
        VcpState.CHARGING,       # Charge started
        VcpState.AVAILABLE,      # Auth expired, EV unplug
        VcpState.PREPARING,      # De-authorize
        VcpState.FAULTED,
        VcpState.OFFLINE,
    },
    VcpState.CHARGING: {
        VcpState.SUSPENDED,      # Paused by EV or EVSE
        VcpState.FINISHING,      # Stop requested / EV unplug
        VcpState.FAULTED,
        VcpState.OFFLINE,
    },
    VcpState.SUSPENDED: {
        VcpState.CHARGING,       # Resume
        VcpState.FINISHING,      # Stop while suspended
        VcpState.FAULTED,
        VcpState.OFFLINE,
    },
    VcpState.FINISHING: {
        VcpState.AVAILABLE,      # Transaction fully stopped
        VcpState.FAULTED,
        VcpState.OFFLINE,
    },
    VcpState.FAULTED: {
        VcpState.RECOVERING,
        VcpState.OFFLINE,
    },
    VcpState.RECOVERING: {
        VcpState.AVAILABLE,
        VcpState.FAULTED,
        VcpState.OFFLINE,
    },
    VcpState.OFFLINE: {
        VcpState.RECOVERING,     # Reconnected — needs boot cycle
        VcpState.AVAILABLE,      # Immediately available after reconnect (some profiles)
    },
}


@dataclass
class StateChangeEvent:
    vcp_id: str
    from_state: VcpState
    to_state: VcpState
    reason: str = ""


# Listener: called after every confirmed state change
StateListener = Callable[[StateChangeEvent], Coroutine[Any, Any, None]]


class ConnectorState:
    """Tracks per-connector runtime state."""

    def __init__(self, connector_id: int, evse_id: int = 1) -> None:
        self.connector_id = connector_id
        self.evse_id = evse_id
        self.state: VcpState = VcpState.AVAILABLE
        self.id_tag: str = ""
        self.transaction_id: str | int | None = None
        self.meter_start_wh: float = 0.0
        self.meter_current_wh: float = 0.0
        self.is_ev_connected: bool = False
        self.is_locked: bool = False
        # Set by ChangeAvailability(Inoperative) / change_state(Unavailable);
        # the connector is then held in FAULTED but reported as "unavailable".
        self.is_unavailable: bool = False


class VcpStateMachine:
    """State machine for one VCP instance (handles all its connectors).

    The primary connector (EVSE 1, connector 1) drives the top-level VCP state
    used for reporting and command routing. Multi-connector EVSEs maintain
    per-connector state independently.
    """

    def __init__(self, vcp_id: str, connector_count: int = 1, evse_count: int = 1) -> None:
        self._vcp_id = vcp_id
        self._lock = asyncio.Lock()
        self._listeners: list[StateListener] = []
        # Called when the WebSocket/boot connection state changes (not legality-checked)
        self._connection_listeners: list[StateListener] = []

        # Build connector state objects
        self._connectors: dict[tuple[int, int], ConnectorState] = {}
        for evse_id in range(1, evse_count + 1):
            for conn_id in range(1, connector_count + 1):
                key = (evse_id, conn_id)
                self._connectors[key] = ConnectorState(connector_id=conn_id, evse_id=evse_id)

        # Top-level VCP connection state (separate from charger states)
        self._connection_state: VcpState = VcpState.OFFLINE

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def primary(self) -> ConnectorState:
        """Convenience: primary connector (EVSE 1, Connector 1)."""
        return self._connectors[(1, 1)]

    @property
    def connection_state(self) -> VcpState:
        return self._connection_state

    def get_connector(self, evse_id: int = 1, connector_id: int = 1) -> ConnectorState:
        key = (evse_id, connector_id)
        if key not in self._connectors:
            raise KeyError(f"No connector ({evse_id}, {connector_id}) in VCP {self._vcp_id}")
        return self._connectors[key]

    def add_listener(self, listener: StateListener) -> None:
        self._listeners.append(listener)

    def add_connection_listener(self, listener: StateListener) -> None:
        """Register a listener fired when the connection state actually changes."""
        self._connection_listeners.append(listener)

    @property
    def vcp_id(self) -> str:
        return self._vcp_id

    async def transition(
        self,
        to_state: VcpState,
        reason: str = "",
        evse_id: int = 1,
        connector_id: int = 1,
    ) -> None:
        """Request a state transition.  Raises StateTransitionError if illegal."""
        async with self._lock:
            connector = self.get_connector(evse_id, connector_id)
            from_state = connector.state
            self._validate_transition(from_state, to_state)
            connector.state = to_state
            logger.info(
                "[%s] state %s → %s (evse=%d conn=%d) reason=%s",
                self._vcp_id, from_state.value, to_state.value, evse_id, connector_id, reason or "—",
            )
            event = StateChangeEvent(
                vcp_id=self._vcp_id,
                from_state=from_state,
                to_state=to_state,
                reason=reason,
            )

        # Fire listeners outside the lock to prevent deadlocks
        await self._notify(event)

    async def force_transition(
        self,
        to_state: VcpState,
        reason: str = "",
        evse_id: int = 1,
        connector_id: int = 1,
    ) -> None:
        """Force a state change bypassing legality checks (fault injection / recovery)."""
        async with self._lock:
            connector = self.get_connector(evse_id, connector_id)
            from_state = connector.state
            connector.state = to_state
            logger.warning(
                "[%s] FORCED state %s → %s reason=%s",
                self._vcp_id, from_state.value, to_state.value, reason or "—",
            )
            event = StateChangeEvent(self._vcp_id, from_state, to_state, reason)

        await self._notify(event)

    async def set_connection_state(self, state: VcpState, reason: str = "") -> None:
        """Update the WebSocket connection state (Offline / Recovering / Available)."""
        old = self._connection_state
        self._connection_state = state
        logger.info("[%s] connection state %s → %s %s", self._vcp_id, old.value, state.value, reason)
        if old != state and self._connection_listeners:
            event = StateChangeEvent(self._vcp_id, old, state, f"connection:{reason}" if reason else "connection")
            for listener in self._connection_listeners:
                try:
                    await listener(event)
                except Exception:  # noqa: BLE001
                    logger.exception("[%s] connection listener error", self._vcp_id)

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-serialisable state snapshot for the registry / API."""
        connectors = {}
        for (evse_id, conn_id), c in self._connectors.items():
            key = f"evse{evse_id}_conn{conn_id}"
            connectors[key] = {
                "evse_id": evse_id,
                "connector_id": conn_id,
                "state": c.state.value,
                "ev_connected": c.is_ev_connected,
                "id_tag": c.id_tag,
                "transaction_id": c.transaction_id,
                "meter_wh": round(c.meter_current_wh, 2),
                "unavailable": c.is_unavailable,
            }
        return {
            "vcp_id": self._vcp_id,
            "connection": self._connection_state.value,
            "connectors": connectors,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _validate_transition(self, from_state: VcpState, to_state: VcpState) -> None:
        allowed = _LEGAL_TRANSITIONS.get(from_state, set())
        if to_state not in allowed:
            raise StateTransitionError(from_state, to_state)

    async def _notify(self, event: StateChangeEvent) -> None:
        for listener in self._listeners:
            try:
                await listener(event)
            except Exception:  # noqa: BLE001
                logger.exception("[%s] state listener error", self._vcp_id)
