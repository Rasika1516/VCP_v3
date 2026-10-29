"""Unit tests for OCPP 1.6J message builders and router."""
from __future__ import annotations

import asyncio
import json
import pytest

from vcp_data_plane.ocpp.common import (
    OcppCall,
    OcppCallResult,
    OcppCallError,
    parse_message,
)
from vcp_data_plane.ocpp.v16.messages import (
    boot_notification,
    heartbeat,
    status_notification,
    authorize,
    start_transaction,
    stop_transaction,
    meter_values,
    build_sampled_value,
    build_meter_value_entry,
    BootNotificationResponse,
    AuthorizeResponse,
    StartTransactionResponse,
)
from vcp_data_plane.ocpp.router import OcppRouter


# ---------------------------------------------------------------------------
# Message parsing
# ---------------------------------------------------------------------------

def test_parse_call() -> None:
    raw = json.dumps([2, "abc123", "BootNotification", {"chargePointVendor": "Test", "chargePointModel": "M1"}])
    msg = parse_message(raw)
    assert isinstance(msg, OcppCall)
    assert msg.message_id == "abc123"
    assert msg.action == "BootNotification"


def test_parse_callresult() -> None:
    raw = json.dumps([3, "abc123", {"status": "Accepted", "currentTime": "2024-01-01T00:00:00Z", "interval": 300}])
    msg = parse_message(raw)
    assert isinstance(msg, OcppCallResult)
    assert msg.message_id == "abc123"
    assert msg.payload["status"] == "Accepted"


def test_parse_callerror() -> None:
    raw = json.dumps([4, "abc123", "NotImplemented", "Action not supported", {}])
    msg = parse_message(raw)
    assert isinstance(msg, OcppCallError)
    assert msg.error_code == "NotImplemented"


def test_parse_invalid_message_type_raises() -> None:
    raw = json.dumps([9, "id", "Unknown", {}])
    with pytest.raises(ValueError):
        parse_message(raw)


def test_parse_non_json_raises() -> None:
    with pytest.raises((json.JSONDecodeError, ValueError)):
        parse_message("not json at all")


# ---------------------------------------------------------------------------
# OCPP 1.6J message builders — payload structure
# ---------------------------------------------------------------------------

def test_boot_notification_required_fields() -> None:
    payload = boot_notification(vendor="Vendor", model="Model")
    assert payload["chargePointVendor"] == "Vendor"
    assert payload["chargePointModel"] == "Model"


def test_boot_notification_optional_firmware_serial() -> None:
    payload = boot_notification(vendor="V", model="M", firmware="2.0", serial="SN-001")
    assert payload.get("firmwareVersion") == "2.0"
    assert payload.get("chargePointSerialNumber") == "SN-001"


def test_boot_notification_omits_empty_optional_fields() -> None:
    payload = boot_notification(vendor="V", model="M")
    assert "firmwareVersion" not in payload
    assert "chargePointSerialNumber" not in payload


def test_heartbeat_payload_is_empty_dict() -> None:
    payload = heartbeat()
    assert payload == {}


def test_status_notification_required_fields() -> None:
    payload = status_notification(connector_id=1, status="Available")
    assert payload["connectorId"] == 1
    assert payload["status"] == "Available"
    assert payload["errorCode"] == "NoError"   # default
    assert "timestamp" in payload


def test_status_notification_custom_error_code() -> None:
    payload = status_notification(connector_id=1, status="Faulted", error_code="GroundFailure")
    assert payload["errorCode"] == "GroundFailure"


def test_authorize_payload() -> None:
    payload = authorize(id_tag="RFID-ABC")
    assert payload["idTag"] == "RFID-ABC"


def test_start_transaction_required_fields() -> None:
    payload = start_transaction(connector_id=1, id_tag="RFID-001", meter_start=0)
    assert payload["connectorId"] == 1
    assert payload["idTag"] == "RFID-001"
    assert payload["meterStart"] == 0
    assert "timestamp" in payload


def test_start_transaction_optional_reservation_id() -> None:
    payload = start_transaction(connector_id=1, id_tag="TAG", meter_start=0, reservation_id=7)
    assert payload.get("reservationId") == 7


def test_stop_transaction_required_fields() -> None:
    payload = stop_transaction(transaction_id=42, meter_stop=5000)
    assert payload["transactionId"] == 42
    assert payload["meterStop"] == 5000
    assert payload["reason"] == "Local"
    assert "timestamp" in payload


def test_stop_transaction_custom_reason() -> None:
    payload = stop_transaction(transaction_id=42, meter_stop=100, reason="Remote")
    assert payload["reason"] == "Remote"


def test_meter_values_payload_structure() -> None:
    sampled = [build_sampled_value("3700", "Power.Active.Import", "W")]
    mv_entry = build_meter_value_entry(sampled)
    payload = meter_values(connector_id=1, meter_value_list=[mv_entry], transaction_id=42)
    assert payload["connectorId"] == 1
    assert payload["transactionId"] == 42
    assert len(payload["meterValue"]) == 1
    assert payload["meterValue"][0]["sampledValue"] == sampled


def test_meter_values_without_transaction_id() -> None:
    payload = meter_values(connector_id=0, meter_value_list=[])
    assert "transactionId" not in payload


def test_build_sampled_value_structure() -> None:
    sv = build_sampled_value("1234.5", "Energy.Active.Import.Register", "Wh")
    assert sv["value"] == "1234.5"
    assert sv["measurand"] == "Energy.Active.Import.Register"
    assert sv["unit"] == "Wh"
    assert "context" in sv


def test_build_meter_value_entry_structure() -> None:
    svs = [build_sampled_value("100", "Energy.Active.Import.Register", "Wh")]
    entry = build_meter_value_entry(svs)
    assert "timestamp" in entry
    assert entry["sampledValue"] == svs


# ---------------------------------------------------------------------------
# Response parsers
# ---------------------------------------------------------------------------

def test_boot_notification_response_accepted() -> None:
    raw = {"status": "Accepted", "currentTime": "2024-01-01T00:00:00Z", "interval": 300}
    resp = BootNotificationResponse(raw)
    assert resp.status == "Accepted"
    assert resp.interval == 300
    assert resp.accepted is True


def test_boot_notification_response_pending() -> None:
    raw = {"status": "Pending", "currentTime": "2024-01-01T00:00:00Z", "interval": 60}
    resp = BootNotificationResponse(raw)
    assert resp.status == "Pending"
    assert resp.accepted is False


def test_boot_notification_response_rejected_by_default() -> None:
    resp = BootNotificationResponse({})  # missing status → defaults to Rejected
    assert resp.accepted is False


def test_authorize_response_accepted() -> None:
    raw = {"idTagInfo": {"status": "Accepted"}}
    resp = AuthorizeResponse(raw)
    assert resp.accepted is True
    assert resp.status == "Accepted"


def test_authorize_response_invalid() -> None:
    raw = {"idTagInfo": {"status": "Invalid"}}
    resp = AuthorizeResponse(raw)
    assert resp.accepted is False


def test_authorize_response_blocked() -> None:
    raw = {"idTagInfo": {"status": "Blocked"}}
    resp = AuthorizeResponse(raw)
    assert resp.accepted is False


def test_start_transaction_response_accepted() -> None:
    raw = {"transactionId": 99, "idTagInfo": {"status": "Accepted"}}
    resp = StartTransactionResponse(raw)
    assert resp.transaction_id == 99
    assert resp.accepted is True


def test_start_transaction_response_rejected() -> None:
    raw = {"transactionId": 0, "idTagInfo": {"status": "Blocked"}}
    resp = StartTransactionResponse(raw)
    assert resp.accepted is False


# ---------------------------------------------------------------------------
# OcppRouter — call/response correlation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_router_call_resolves_on_callresult() -> None:
    sent_messages: list[str] = []

    async def fake_send(raw: str) -> None:
        sent_messages.append(raw)

    router = OcppRouter(vcp_id="CP-TEST")
    router.set_send_fn(fake_send)

    async def inject_response() -> None:
        await asyncio.sleep(0.02)
        sent = json.loads(sent_messages[0])
        msg_id = sent[1]
        callresult = json.dumps([3, msg_id, {"status": "Accepted", "currentTime": "2024-01-01T00:00:00Z", "interval": 300}])
        await router.handle_inbound(callresult)

    asyncio.create_task(inject_response())
    result = await router.call("BootNotification", {"chargePointVendor": "T", "chargePointModel": "M"})

    assert isinstance(result, OcppCallResult)
    assert result.payload["status"] == "Accepted"


@pytest.mark.asyncio
async def test_router_registers_inbound_handler() -> None:
    received: list[OcppCall] = []

    async def fake_send(raw: str) -> None:
        pass

    async def handler(call: OcppCall) -> dict:
        received.append(call)
        return {"status": "Accepted"}

    router = OcppRouter(vcp_id="CP-TEST")
    router.set_send_fn(fake_send)
    router.register("RemoteStartTransaction", handler)

    inbound = json.dumps([2, "msg-001", "RemoteStartTransaction", {"connectorId": 1, "idTag": "TAG"}])
    await router.handle_inbound(inbound)

    assert len(received) == 1
    assert received[0].action == "RemoteStartTransaction"


@pytest.mark.asyncio
async def test_router_callerror_resolves_pending() -> None:
    sent_messages: list[str] = []

    async def fake_send(raw: str) -> None:
        sent_messages.append(raw)

    router = OcppRouter(vcp_id="CP-TEST")
    router.set_send_fn(fake_send)

    async def inject_error() -> None:
        await asyncio.sleep(0.02)
        sent = json.loads(sent_messages[0])
        msg_id = sent[1]
        err = json.dumps([4, msg_id, "NotImplemented", "Not supported", {}])
        await router.handle_inbound(err)

    asyncio.create_task(inject_error())
    result = await router.call("SomeAction", {})
    assert isinstance(result, OcppCallError)
    assert result.error_code == "NotImplemented"


@pytest.mark.asyncio
async def test_router_send_encodes_call_frame() -> None:
    sent_messages: list[str] = []
    router = OcppRouter(vcp_id="CP-TEST")

    async def fake_send(raw: str) -> None:
        sent_messages.append(raw)
        # Immediately resolve to avoid timeout
        parsed = json.loads(raw)
        if parsed[0] == 2:
            callresult = json.dumps([3, parsed[1], {}])
            await router.handle_inbound(callresult)

    router.set_send_fn(fake_send)
    await router.call("Heartbeat", {})

    assert len(sent_messages) == 1
    frame = json.loads(sent_messages[0])
    assert frame[0] == 2           # CALL type
    assert frame[2] == "Heartbeat"
    assert frame[3] == {}
