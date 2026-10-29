"""Unit tests for FaultInjector."""
from __future__ import annotations

import asyncio
import pytest

from vcp_data_plane.fault.injector import FaultInjector, FaultType, FaultResult
from vcp_data_plane.state.machine import VcpStateMachine, VcpState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_sm(vcp_id: str = "CP-TEST") -> VcpStateMachine:
    return VcpStateMachine(vcp_id=vcp_id)


def _make_injector(vcp_id: str = "CP-TEST") -> FaultInjector:
    sm = _make_sm(vcp_id)
    return FaultInjector(vcp_id=vcp_id, state_machine=sm)


# ---------------------------------------------------------------------------
# FaultResult structure
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fault_result_has_vcp_id() -> None:
    inj = _make_injector("VCP-99")
    result = await inj.inject(FaultType.CONNECTOR_FAULT)
    assert result.vcp_id == "VCP-99"


@pytest.mark.asyncio
async def test_fault_result_has_fault_type() -> None:
    inj = _make_injector()
    result = await inj.inject(FaultType.CONNECTOR_FAULT)
    assert result.fault_type == FaultType.CONNECTOR_FAULT


@pytest.mark.asyncio
async def test_fault_result_has_injected_bool() -> None:
    inj = _make_injector()
    result = await inj.inject(FaultType.CONNECTOR_FAULT)
    # CONNECTOR_FAULT transitions state machine — injected should be True
    assert isinstance(result.injected, bool)


@pytest.mark.asyncio
async def test_fault_result_has_message() -> None:
    inj = _make_injector()
    result = await inj.inject(FaultType.CONNECTOR_FAULT)
    assert isinstance(result.message, str)


# ---------------------------------------------------------------------------
# Connector fault — state machine transition
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_connector_fault_transitions_to_faulted() -> None:
    sm = _make_sm()
    inj = FaultInjector(vcp_id="CP", state_machine=sm)
    result = await inj.inject(FaultType.CONNECTOR_FAULT, {"connector_id": 1})
    assert result.injected is True
    assert sm.primary.state == VcpState.FAULTED


@pytest.mark.asyncio
async def test_connector_fault_custom_error_code() -> None:
    sm = _make_sm()
    inj = FaultInjector(vcp_id="CP", state_machine=sm)
    result = await inj.inject(FaultType.CONNECTOR_FAULT, {"connector_id": 1, "error_code": "GroundFailure"})
    assert result.injected is True
    assert "GroundFailure" in result.message


# ---------------------------------------------------------------------------
# Message timeout
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_message_timeout_sets_intercept_flag() -> None:
    inj = _make_injector()
    result = await inj.inject(FaultType.MESSAGE_TIMEOUT, {"duration_s": 100.0})
    assert result.injected is True
    assert inj.is_message_timeout_active() is True
    assert inj.intercept_inbound is True


@pytest.mark.asyncio
async def test_message_timeout_clears_after_duration() -> None:
    inj = _make_injector()
    await inj.inject(FaultType.MESSAGE_TIMEOUT, {"duration_s": 0.05})
    assert inj.is_message_timeout_active() is True
    await asyncio.sleep(0.1)
    assert inj.is_message_timeout_active() is False


# ---------------------------------------------------------------------------
# Slow response
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_slow_response_sets_delay() -> None:
    inj = _make_injector()
    result = await inj.inject(FaultType.SLOW_RESPONSE, {"delay_s": 3.0, "duration_s": 60.0})
    assert result.injected is True
    assert inj.get_response_delay() == pytest.approx(3.0)


@pytest.mark.asyncio
async def test_slow_response_delay_clears_after_duration() -> None:
    inj = _make_injector()
    await inj.inject(FaultType.SLOW_RESPONSE, {"delay_s": 5.0, "duration_s": 0.05})
    await asyncio.sleep(0.1)
    assert inj.get_response_delay() == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Network disconnect — requires bound stop_fn
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_network_disconnect_without_bind_returns_not_injected() -> None:
    inj = _make_injector()
    result = await inj.inject(FaultType.NETWORK_DISCONNECT)
    # No stop_fn bound → should not fail fatally, just report failure
    assert result.fault_type == FaultType.NETWORK_DISCONNECT
    assert result.injected is False


@pytest.mark.asyncio
async def test_network_disconnect_with_bound_stop_fn() -> None:
    stop_called = False

    async def fake_stop() -> None:
        nonlocal stop_called
        stop_called = True

    sm = _make_sm()
    inj = FaultInjector(vcp_id="CP", state_machine=sm)
    inj.bind(send_fn=None, stop_fn=fake_stop, start_fn=None)
    result = await inj.inject(FaultType.NETWORK_DISCONNECT)
    assert result.injected is True
    assert stop_called is True


# ---------------------------------------------------------------------------
# Malformed payload — requires bound send_fn
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_malformed_payload_without_bind_returns_not_injected() -> None:
    inj = _make_injector()
    result = await inj.inject(FaultType.MALFORMED_PAYLOAD)
    assert result.injected is False


@pytest.mark.asyncio
async def test_malformed_payload_with_send_fn() -> None:
    sent: list[str] = []

    async def fake_send(raw: str) -> None:
        sent.append(raw)

    sm = _make_sm()
    inj = FaultInjector(vcp_id="CP", state_machine=sm)
    inj.bind(send_fn=fake_send, stop_fn=None, start_fn=None)
    result = await inj.inject(FaultType.MALFORMED_PAYLOAD)
    assert result.injected is True
    assert len(sent) == 1
    # Payload must actually be malformed
    import json
    with pytest.raises(json.JSONDecodeError):
        json.loads(sent[0])


# ---------------------------------------------------------------------------
# Duplicate messages — requires bound send_fn
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_duplicate_messages_sends_twice() -> None:
    sent: list[str] = []

    async def fake_send(raw: str) -> None:
        sent.append(raw)

    sm = _make_sm()
    inj = FaultInjector(vcp_id="CP", state_machine=sm)
    inj.bind(send_fn=fake_send, stop_fn=None, start_fn=None)
    result = await inj.inject(FaultType.DUPLICATE_MESSAGES)
    assert result.injected is True
    assert len(sent) == 2
    assert sent[0] == sent[1]


# ---------------------------------------------------------------------------
# Recover
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_recover_clears_message_timeout() -> None:
    inj = _make_injector()
    await inj.inject(FaultType.MESSAGE_TIMEOUT, {"duration_s": 100.0})
    assert inj.is_message_timeout_active() is True
    await inj.recover()
    assert inj.is_message_timeout_active() is False
    assert inj.intercept_inbound is False


@pytest.mark.asyncio
async def test_recover_clears_slow_response() -> None:
    inj = _make_injector()
    await inj.inject(FaultType.SLOW_RESPONSE, {"delay_s": 10.0, "duration_s": 100.0})
    assert inj.get_response_delay() > 0
    await inj.recover()
    assert inj.get_response_delay() == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_recover_after_connector_fault_transitions_to_available() -> None:
    sm = _make_sm()
    inj = FaultInjector(vcp_id="CP", state_machine=sm)
    await inj.inject(FaultType.CONNECTOR_FAULT)
    assert sm.primary.state == VcpState.FAULTED
    await inj.recover()
    # After recovery, SM should have transitioned out of Faulted
    assert sm.primary.state in (VcpState.RECOVERING, VcpState.AVAILABLE)


# ---------------------------------------------------------------------------
# All fault types can be invoked
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("fault_type", list(FaultType))
async def test_all_fault_types_return_fault_result(fault_type: FaultType) -> None:
    inj = _make_injector()
    result = await inj.inject(fault_type, {})
    assert isinstance(result, FaultResult)
    assert result.fault_type == fault_type
