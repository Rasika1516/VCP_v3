"""OCPP 1.6J message payload builders and response parsers.

These are pure-data functions: they build the dict payloads that go into
OcppCall.payload and parse the dicts that come back in OcppCallResult.payload.
No I/O or state mutation happens here.

Reference: OCPP 1.6J specification, ERRATA 2019-01.
"""
from __future__ import annotations

import datetime
from typing import Any


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Charge Point → CSMS  (outgoing from the VCP)
# ---------------------------------------------------------------------------

def boot_notification(vendor: str, model: str, firmware: str = "", serial: str = "") -> dict[str, Any]:
    payload: dict[str, Any] = {
        "chargePointVendor": vendor,
        "chargePointModel": model,
    }
    if firmware:
        payload["firmwareVersion"] = firmware
    if serial:
        payload["chargePointSerialNumber"] = serial
    return payload


def heartbeat() -> dict[str, Any]:
    return {}


def status_notification(
    connector_id: int,
    status: str,
    error_code: str = "NoError",
    info: str = "",
    vendor_id: str = "",
    vendor_error_code: str = "",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "connectorId": connector_id,
        "status": status,
        "errorCode": error_code,
        "timestamp": _now_iso(),
    }
    if info:
        payload["info"] = info
    if vendor_id:
        payload["vendorId"] = vendor_id
    if vendor_error_code:
        payload["vendorErrorCode"] = vendor_error_code
    return payload


def authorize(id_tag: str) -> dict[str, Any]:
    return {"idTag": id_tag}


def start_transaction(
    connector_id: int,
    id_tag: str,
    meter_start: int,
    reservation_id: int | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "connectorId": connector_id,
        "idTag": id_tag,
        "meterStart": meter_start,
        "timestamp": _now_iso(),
    }
    if reservation_id is not None:
        payload["reservationId"] = reservation_id
    return payload


def stop_transaction(
    transaction_id: int,
    meter_stop: int,
    reason: str = "Local",
    id_tag: str | None = None,
    transaction_data: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "transactionId": transaction_id,
        "meterStop": meter_stop,
        "timestamp": _now_iso(),
        "reason": reason,
    }
    if id_tag:
        payload["idTag"] = id_tag
    if transaction_data:
        payload["transactionData"] = transaction_data
    return payload


def meter_values(
    connector_id: int,
    meter_value_list: list[dict[str, Any]],
    transaction_id: int | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "connectorId": connector_id,
        "meterValue": meter_value_list,
    }
    if transaction_id is not None:
        payload["transactionId"] = transaction_id
    return payload


def build_sampled_value(
    value: str,
    measurand: str = "Energy.Active.Import.Register",
    unit: str = "Wh",
    context: str = "Sample.Periodic",
    location: str = "Outlet",
    phase: str | None = None,
) -> dict[str, Any]:
    sv: dict[str, Any] = {
        "value": value,
        "context": context,
        "measurand": measurand,
        "unit": unit,
        "location": location,
    }
    if phase:
        sv["phase"] = phase
    return sv


def build_meter_value_entry(
    sampled_values: list[dict[str, Any]],
    timestamp: str | None = None,
) -> dict[str, Any]:
    return {
        "timestamp": timestamp or _now_iso(),
        "sampledValue": sampled_values,
    }


def data_transfer(vendor_id: str, message_id: str = "", data: str = "") -> dict[str, Any]:
    payload: dict[str, Any] = {"vendorId": vendor_id}
    if message_id:
        payload["messageId"] = message_id
    if data:
        payload["data"] = data
    return payload


def diagnostics_status_notification(status: str) -> dict[str, Any]:
    return {"status": status}


def firmware_status_notification(status: str) -> dict[str, Any]:
    return {"status": status}


# ---------------------------------------------------------------------------
# Response parsers (CSMS → CP)
# ---------------------------------------------------------------------------

class BootNotificationResponse:
    """Parsed BootNotification response from CSMS."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.status: str = payload.get("status", "Rejected")
        self.current_time: str = payload.get("currentTime", _now_iso())
        self.interval: int = payload.get("interval", 60)

    @property
    def accepted(self) -> bool:
        return self.status == "Accepted"


class AuthorizeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        id_tag_info = payload.get("idTagInfo", {})
        self.status: str = id_tag_info.get("status", "Invalid")
        self.expiry_date: str | None = id_tag_info.get("expiryDate")

    @property
    def accepted(self) -> bool:
        return self.status == "Accepted"


class StartTransactionResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.transaction_id: int = payload.get("transactionId", 0)
        id_tag_info = payload.get("idTagInfo", {})
        self.status: str = id_tag_info.get("status", "Invalid")

    @property
    def accepted(self) -> bool:
        return self.status == "Accepted"


# ---------------------------------------------------------------------------
# CSMS → CP action payloads (for inbound handler responses)
# ---------------------------------------------------------------------------

class RemoteStartTransactionRequest:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.connector_id: int | None = payload.get("connectorId")
        self.id_tag: str = payload.get("idTag", "")
        self.charging_profile: dict[str, Any] | None = payload.get("chargingProfile")


class RemoteStopTransactionRequest:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.transaction_id: int = payload.get("transactionId", 0)


class ChangeAvailabilityRequest:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.connector_id: int = payload.get("connectorId", 0)
        self.availability_type: str = payload.get("type", "Operative")  # Inoperative | Operative


class ChangeConfigurationRequest:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.key: str = payload.get("key", "")
        self.value: str = payload.get("value", "")


class GetConfigurationRequest:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.key: list[str] = payload.get("key", [])


class ResetRequest:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.reset_type: str = payload.get("type", "Soft")  # Soft | Hard


class UnlockConnectorRequest:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.connector_id: int = payload.get("connectorId", 1)


class TriggerMessageRequest:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.requested_message: str = payload.get("requestedMessage", "")
        self.connector_id: int | None = payload.get("connectorId")
