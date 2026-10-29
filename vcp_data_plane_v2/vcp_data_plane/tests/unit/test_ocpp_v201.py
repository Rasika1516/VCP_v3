"""Unit tests for OCPP 2.0.1 message builders and response parsers."""
from __future__ import annotations

import pytest

from vcp_data_plane.ocpp.v201.messages import (
    boot_notification,
    transaction_event,
    authorize,
    BootNotificationResponse,
    AuthorizeResponse,
    TransactionEventResponse,
)


# ---------------------------------------------------------------------------
# BootNotification (2.0.1)
# ---------------------------------------------------------------------------

def test_boot_notification_required_fields() -> None:
    payload = boot_notification(
        reason="PowerUp",
        vendor_name="TestVendor",
        model="TestModel",
    )
    assert payload["reason"] == "PowerUp"
    assert payload["chargingStation"]["vendorName"] == "TestVendor"
    assert payload["chargingStation"]["model"] == "TestModel"


def test_boot_notification_with_firmware_version() -> None:
    payload = boot_notification(
        reason="FirmwareUpdate",
        vendor_name="VND",
        model="MDL",
        firmware_version="3.1.0",
    )
    assert payload["chargingStation"]["firmwareVersion"] == "3.1.0"


def test_boot_notification_with_serial_number() -> None:
    payload = boot_notification(
        reason="PowerUp",
        vendor_name="VND",
        model="MDL",
        serial_number="SN-123",
    )
    assert payload["chargingStation"]["serialNumber"] == "SN-123"


def test_boot_notification_omits_empty_optional_fields() -> None:
    payload = boot_notification(reason="PowerUp", vendor_name="VND", model="MDL")
    cs = payload["chargingStation"]
    assert "firmwareVersion" not in cs
    assert "serialNumber" not in cs


# ---------------------------------------------------------------------------
# TransactionEvent (2.0.1)
# ---------------------------------------------------------------------------

def test_transaction_event_started_required_fields() -> None:
    payload = transaction_event(
        event_type="Started",
        timestamp="2024-01-01T00:00:00Z",
        trigger_reason="Authorized",
        seq_no=0,
        transaction_id="TX-UUID-001",
        evse_id=1,
        connector_id=1,
    )
    assert payload["eventType"] == "Started"
    assert payload["triggerReason"] == "Authorized"
    assert payload["seqNo"] == 0
    assert payload["transactionInfo"]["transactionId"] == "TX-UUID-001"
    assert payload["evse"]["id"] == 1
    assert payload["evse"]["connectorId"] == 1
    assert payload["timestamp"] == "2024-01-01T00:00:00Z"


def test_transaction_event_with_id_token() -> None:
    payload = transaction_event(
        event_type="Started",
        timestamp="2024-01-01T00:00:00Z",
        trigger_reason="Authorized",
        seq_no=0,
        transaction_id="TX-001",
        evse_id=1,
        connector_id=1,
        id_token="RFID-001",
        id_token_type="ISO14443",
    )
    assert payload["idToken"]["idToken"] == "RFID-001"
    assert payload["idToken"]["type"] == "ISO14443"


def test_transaction_event_updated_no_id_token() -> None:
    payload = transaction_event(
        event_type="Updated",
        timestamp="2024-01-01T00:30:00Z",
        trigger_reason="MeterValuePeriodic",
        seq_no=5,
        transaction_id="TX-UUID-001",
        evse_id=1,
        connector_id=1,
    )
    assert payload["eventType"] == "Updated"
    assert "idToken" not in payload


def test_transaction_event_ended_with_stopped_reason() -> None:
    payload = transaction_event(
        event_type="Ended",
        timestamp="2024-01-01T01:00:00Z",
        trigger_reason="StopAuthorized",
        seq_no=10,
        transaction_id="TX-UUID-001",
        evse_id=1,
        connector_id=1,
        stopped_reason="Local",
    )
    assert payload["transactionInfo"]["stoppedReason"] == "Local"


def test_transaction_event_seq_no_increments() -> None:
    payloads = []
    for i in range(5):
        p = transaction_event(
            event_type="Updated",
            timestamp="2024-01-01T00:00:00Z",
            trigger_reason="MeterValuePeriodic",
            seq_no=i,
            transaction_id="TX-001",
            evse_id=1,
            connector_id=1,
        )
        payloads.append(p)

    seq_nos = [p["seqNo"] for p in payloads]
    assert seq_nos == list(range(5))


def test_transaction_event_with_meter_values() -> None:
    mv = [{"timestamp": "2024-01-01T00:00:00Z", "sampledValue": [{"value": 5.0, "measurand": "Power.Active.Import"}]}]
    payload = transaction_event(
        event_type="Updated",
        timestamp="2024-01-01T00:00:00Z",
        trigger_reason="MeterValuePeriodic",
        seq_no=1,
        transaction_id="TX-001",
        evse_id=1,
        connector_id=1,
        meter_values=mv,
    )
    assert "meterValue" in payload
    assert len(payload["meterValue"]) == 1


def test_transaction_event_offline_flag() -> None:
    payload = transaction_event(
        event_type="Started",
        timestamp="2024-01-01T00:00:00Z",
        trigger_reason="EVCommunicationLost",
        seq_no=0,
        transaction_id="TX-999",
        evse_id=1,
        connector_id=1,
        offline=True,
    )
    assert payload["offline"] is True


# ---------------------------------------------------------------------------
# Authorize (2.0.1)
# ---------------------------------------------------------------------------

def test_authorize_payload_structure() -> None:
    payload = authorize(id_token="TAG-001", id_token_type="ISO14443")
    assert payload["idToken"]["idToken"] == "TAG-001"
    assert payload["idToken"]["type"] == "ISO14443"


def test_authorize_default_type_is_central() -> None:
    payload = authorize(id_token="TAG-001")
    assert payload["idToken"]["type"] == "Central"


# ---------------------------------------------------------------------------
# Response parsers (2.0.1)
# ---------------------------------------------------------------------------

def test_boot_notification_response_accepted() -> None:
    raw = {
        "currentTime": "2024-01-01T00:00:00Z",
        "interval": 300,
        "status": "Accepted",
    }
    resp = BootNotificationResponse(raw)
    assert resp.status == "Accepted"
    assert resp.interval == 300
    assert resp.accepted is True


def test_boot_notification_response_rejected() -> None:
    raw = {
        "currentTime": "2024-01-01T00:00:00Z",
        "interval": 60,
        "status": "Rejected",
    }
    resp = BootNotificationResponse(raw)
    assert resp.accepted is False


def test_boot_notification_response_default_rejected() -> None:
    resp = BootNotificationResponse({})
    assert resp.accepted is False


def test_authorize_response_accepted() -> None:
    raw = {
        "idTokenInfo": {
            "status": "Accepted",
        }
    }
    resp = AuthorizeResponse(raw)
    assert resp.accepted is True
    assert resp.status == "Accepted"


def test_authorize_response_blocked() -> None:
    raw = {
        "idTokenInfo": {
            "status": "Blocked",
        }
    }
    resp = AuthorizeResponse(raw)
    assert resp.accepted is False


def test_authorize_response_default_invalid() -> None:
    resp = AuthorizeResponse({})
    assert resp.accepted is False


def test_transaction_event_response_minimal() -> None:
    # OCPP 2.0.1: all fields in TransactionEventResponse are optional
    resp = TransactionEventResponse({})
    assert resp.total_cost is None
    assert resp.charging_priority is None
    assert resp.id_token_status == ""


def test_transaction_event_response_with_total_cost() -> None:
    raw = {
        "idTokenInfo": {"status": "Accepted"},
        "totalCost": 5.50,
    }
    resp = TransactionEventResponse(raw)
    assert resp.total_cost == pytest.approx(5.50)
    assert resp.id_token_status == "Accepted"


def test_transaction_event_response_with_charging_priority() -> None:
    raw = {"chargingPriority": 2}
    resp = TransactionEventResponse(raw)
    assert resp.charging_priority == 2
