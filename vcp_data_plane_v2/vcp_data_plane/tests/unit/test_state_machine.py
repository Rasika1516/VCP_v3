"""Unit tests for VCP State Machine."""
from __future__ import annotations

import asyncio
import pytest

from vcp_data_plane.state.machine import (
    VcpState,
    VcpStateMachine,
    StateTransitionError,
    StateChangeEvent,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_sm(vcp_id: str = "CP-001") -> VcpStateMachine:
    return VcpStateMachine(vcp_id=vcp_id)


# ---------------------------------------------------------------------------
# Initial state
# ---------------------------------------------------------------------------

def test_initial_connector_state_is_available() -> None:
    """Primary connector starts in Available state after object creation."""
    sm = _make_sm()
    assert sm.primary.state == VcpState.AVAILABLE


def test_initial_connection_state_is_offline() -> None:
    sm = _make_sm()
    assert sm.connection_state == VcpState.OFFLINE


def test_initial_snapshot_structure() -> None:
    sm = _make_sm("VCP-X")
    snap = sm.snapshot()
    assert snap["vcp_id"] == "VCP-X"
    assert "connection" in snap
    assert "connectors" in snap
    # Primary connector key is evse1_conn1
    assert "evse1_conn1" in snap["connectors"]


def test_primary_connector_accessible() -> None:
    sm = _make_sm()
    primary = sm.primary
    assert primary.evse_id == 1
    assert primary.connector_id == 1


# ---------------------------------------------------------------------------
# Legal transitions (connector state)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_available_to_preparing() -> None:
    sm = _make_sm()
    await sm.transition(VcpState.PREPARING, reason="plug_in", evse_id=1, connector_id=1)
    assert sm.primary.state == VcpState.PREPARING


@pytest.mark.asyncio
async def test_full_charge_cycle() -> None:
    sm = _make_sm()
    await sm.transition(VcpState.PREPARING, reason="ev_plug")
    await sm.transition(VcpState.AUTHORIZED, reason="authorized")
    await sm.transition(VcpState.CHARGING, reason="start_transaction")
    await sm.transition(VcpState.FINISHING, reason="stop_transaction")
    await sm.transition(VcpState.AVAILABLE, reason="unplugged")
    assert sm.primary.state == VcpState.AVAILABLE


@pytest.mark.asyncio
async def test_charging_to_suspended_and_back() -> None:
    sm = _make_sm()
    await sm.transition(VcpState.PREPARING, reason="ev_connected")
    await sm.transition(VcpState.AUTHORIZED, reason="authorized")
    await sm.transition(VcpState.CHARGING, reason="started")
    await sm.transition(VcpState.SUSPENDED, reason="ev_paused")
    assert sm.primary.state == VcpState.SUSPENDED
    await sm.transition(VcpState.CHARGING, reason="ev_resumed")
    assert sm.primary.state == VcpState.CHARGING


@pytest.mark.asyncio
async def test_available_to_faulted() -> None:
    sm = _make_sm()
    await sm.transition(VcpState.FAULTED, reason="hardware_fault")
    assert sm.primary.state == VcpState.FAULTED


@pytest.mark.asyncio
async def test_faulted_to_recovering() -> None:
    sm = _make_sm()
    await sm.transition(VcpState.FAULTED, reason="fault")
    await sm.transition(VcpState.RECOVERING, reason="self_test")
    assert sm.primary.state == VcpState.RECOVERING


@pytest.mark.asyncio
async def test_available_to_offline() -> None:
    sm = _make_sm()
    await sm.transition(VcpState.OFFLINE, reason="disconnect")
    assert sm.primary.state == VcpState.OFFLINE


@pytest.mark.asyncio
async def test_charging_to_offline() -> None:
    sm = _make_sm()
    await sm.force_transition(VcpState.CHARGING, reason="setup")
    await sm.transition(VcpState.OFFLINE, reason="network_loss")
    assert sm.primary.state == VcpState.OFFLINE


# ---------------------------------------------------------------------------
# Illegal transitions
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_available_to_charging_is_illegal() -> None:
    sm = _make_sm()
    # Can't jump to Charging from Available
    with pytest.raises(StateTransitionError):
        await sm.transition(VcpState.CHARGING, reason="skip_steps")


@pytest.mark.asyncio
async def test_available_to_authorized_without_preparing_is_illegal() -> None:
    sm = _make_sm()
    with pytest.raises(StateTransitionError):
        await sm.transition(VcpState.AUTHORIZED, reason="skip_preparing")


@pytest.mark.asyncio
async def test_finishing_to_charging_is_illegal() -> None:
    sm = _make_sm()
    await sm.force_transition(VcpState.FINISHING, reason="setup")
    with pytest.raises(StateTransitionError):
        await sm.transition(VcpState.CHARGING, reason="illegal")


@pytest.mark.asyncio
async def test_offline_to_charging_is_illegal() -> None:
    sm = _make_sm()
    await sm.force_transition(VcpState.OFFLINE, reason="setup")
    with pytest.raises(StateTransitionError):
        await sm.transition(VcpState.CHARGING, reason="illegal")


# ---------------------------------------------------------------------------
# force_transition bypasses checks
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_force_transition_to_any_state() -> None:
    sm = _make_sm()
    # Jump directly to CHARGING without going through legal steps
    await sm.force_transition(VcpState.CHARGING, reason="test_override")
    assert sm.primary.state == VcpState.CHARGING


@pytest.mark.asyncio
async def test_force_transition_with_evse_connector() -> None:
    sm = VcpStateMachine(vcp_id="CP-002", connector_count=2, evse_count=1)
    await sm.force_transition(VcpState.FAULTED, reason="test", evse_id=1, connector_id=2)
    assert sm.get_connector(1, 2).state == VcpState.FAULTED


# ---------------------------------------------------------------------------
# State listener callbacks
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_state_listener_is_called_on_transition() -> None:
    sm = _make_sm()
    events: list[StateChangeEvent] = []

    async def listener(event: StateChangeEvent) -> None:
        events.append(event)

    sm.add_listener(listener)
    await sm.transition(VcpState.PREPARING, reason="ev_plug")
    await sm.transition(VcpState.AUTHORIZED, reason="authorized")

    assert len(events) == 2
    assert events[0].from_state == VcpState.AVAILABLE
    assert events[0].to_state == VcpState.PREPARING
    assert events[1].to_state == VcpState.AUTHORIZED


@pytest.mark.asyncio
async def test_listener_receives_vcp_id() -> None:
    sm = _make_sm("VCP-99")
    events: list[StateChangeEvent] = []

    async def listener(event: StateChangeEvent) -> None:
        events.append(event)

    sm.add_listener(listener)
    await sm.transition(VcpState.PREPARING, reason="test")
    assert events[0].vcp_id == "VCP-99"


@pytest.mark.asyncio
async def test_listener_receives_reason() -> None:
    sm = _make_sm()
    events: list[StateChangeEvent] = []

    async def listener(event: StateChangeEvent) -> None:
        events.append(event)

    sm.add_listener(listener)
    await sm.transition(VcpState.PREPARING, reason="cable_plugged")
    assert events[0].reason == "cable_plugged"


# ---------------------------------------------------------------------------
# Connection state
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_set_connection_state() -> None:
    sm = _make_sm()
    await sm.set_connection_state(VcpState.AVAILABLE, reason="boot_accepted")
    assert sm.connection_state == VcpState.AVAILABLE


@pytest.mark.asyncio
async def test_connection_state_back_to_offline() -> None:
    sm = _make_sm()
    await sm.set_connection_state(VcpState.AVAILABLE)
    await sm.set_connection_state(VcpState.OFFLINE, reason="disconnect")
    assert sm.connection_state == VcpState.OFFLINE


# ---------------------------------------------------------------------------
# Snapshot details
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_snapshot_reflects_connector_state() -> None:
    sm = _make_sm()
    await sm.transition(VcpState.PREPARING, reason="plug")
    snap = sm.snapshot()
    assert snap["connectors"]["evse1_conn1"]["state"] == VcpState.PREPARING.value


@pytest.mark.asyncio
async def test_snapshot_reflects_transaction_id() -> None:
    sm = _make_sm()
    sm.primary.transaction_id = "TX-9999"
    snap = sm.snapshot()
    assert snap["connectors"]["evse1_conn1"]["transaction_id"] == "TX-9999"


def test_snapshot_multi_connector() -> None:
    sm = VcpStateMachine(vcp_id="MULTI", connector_count=2, evse_count=2)
    snap = sm.snapshot()
    assert "evse1_conn1" in snap["connectors"]
    assert "evse1_conn2" in snap["connectors"]
    assert "evse2_conn1" in snap["connectors"]
    assert "evse2_conn2" in snap["connectors"]


# ---------------------------------------------------------------------------
# get_connector
# ---------------------------------------------------------------------------

def test_get_connector_valid() -> None:
    sm = VcpStateMachine(vcp_id="CP", connector_count=2, evse_count=1)
    c = sm.get_connector(evse_id=1, connector_id=2)
    assert c.connector_id == 2


def test_get_connector_invalid_raises() -> None:
    sm = _make_sm()
    with pytest.raises(KeyError):
        sm.get_connector(evse_id=99, connector_id=99)


# ---------------------------------------------------------------------------
# StateTransitionError attributes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_state_transition_error_has_states() -> None:
    sm = _make_sm()
    try:
        await sm.transition(VcpState.CHARGING, reason="skip")
    except StateTransitionError as exc:
        assert exc.from_state == VcpState.AVAILABLE
        assert exc.to_state == VcpState.CHARGING
    else:
        pytest.fail("Expected StateTransitionError was not raised")
