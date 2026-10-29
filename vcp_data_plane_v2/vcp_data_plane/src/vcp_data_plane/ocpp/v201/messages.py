"""OCPP 2.0.1 message payload builders and response parsers.

OCPP 2.0.1 introduces the device model (components/variables), replaces
Start/StopTransaction with TransactionEvent, and substantially restructures
StatusNotification.

Reference: OCPP 2.0.1 Specification Part 2 (Messages), 2022-12.
"""
from __future__ import annotations

import datetime
import uuid
from typing import Any


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Charge Station → CSMS (outgoing from the VCP)
# ---------------------------------------------------------------------------

def boot_notification(
    reason: str,
    vendor_name: str,
    model: str,
    firmware_version: str = "",
    serial_number: str = "",
) -> dict[str, Any]:
    charging_station: dict[str, Any] = {
        "vendorName": vendor_name,
        "model": model,
    }
    if firmware_version:
        charging_station["firmwareVersion"] = firmware_version
    if serial_number:
        charging_station["serialNumber"] = serial_number
    return {
        "reason": reason,          # PowerUp | ApplicationReset | FirmwareUpdate | …
        "chargingStation": charging_station,
    }


def heartbeat() -> dict[str, Any]:
    return {}


def status_notification(
    timestamp: str,
    connector_status: str,
    evse_id: int,
    connector_id: int,
) -> dict[str, Any]:
    return {
        "timestamp": timestamp,
        "connectorStatus": connector_status,    # Available | Occupied | Faulted | Unavailable | Reserved
        "evseId": evse_id,
        "connectorId": connector_id,
    }


def authorize(id_token: str, id_token_type: str = "Central") -> dict[str, Any]:
    return {
        "idToken": {
            "idToken": id_token,
            "type": id_token_type,
        }
    }


def transaction_event(
    event_type: str,                        # Started | Updated | Ended
    timestamp: str,
    trigger_reason: str,                    # Authorized | CablePluggedIn | RemoteStart | …
    seq_no: int,
    transaction_id: str,
    evse_id: int,
    connector_id: int,
    id_token: str | None = None,
    id_token_type: str = "Central",
    meter_values: list[dict[str, Any]] | None = None,
    offline: bool = False,
    number_of_phases_used: int | None = None,
    cable_max_current: float | None = None,
    reservation_id: int | None = None,
    charging_state: str | None = None,     # EVConnected | Charging | SuspendedEVSE | …
    stopped_reason: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "eventType": event_type,
        "timestamp": timestamp,
        "triggerReason": trigger_reason,
        "seqNo": seq_no,
        "transactionInfo": {
            "transactionId": transaction_id,
        },
        "evse": {
            "id": evse_id,
            "connectorId": connector_id,
        },
        "offline": offline,
    }
    if charging_state:
        payload["transactionInfo"]["chargingState"] = charging_state
    if stopped_reason:
        payload["transactionInfo"]["stoppedReason"] = stopped_reason
    if id_token:
        payload["idToken"] = {"idToken": id_token, "type": id_token_type}
    if meter_values:
        payload["meterValue"] = meter_values
    if number_of_phases_used is not None:
        payload["numberOfPhasesUsed"] = number_of_phases_used
    if cable_max_current is not None:
        payload["cableMaxCurrent"] = cable_max_current
    if reservation_id is not None:
        payload["reservationId"] = reservation_id
    return payload


def meter_values(
    evse_id: int,
    meter_value_list: list[dict[str, Any]],
    custom_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "evseId": evse_id,
        "meterValue": meter_value_list,
    }
    if custom_data:
        payload["customData"] = custom_data
    return payload


def build_sampled_value(
    value: float,
    measurand: str = "Energy.Active.Import.Register",
    unit_of_measure: dict[str, Any] | None = None,
    context: str = "Sample.Periodic",
    location: str = "Outlet",
    phase: str | None = None,
) -> dict[str, Any]:
    sv: dict[str, Any] = {
        "value": value,
        "context": context,
        "measurand": measurand,
        "location": location,
    }
    if unit_of_measure:
        sv["unitOfMeasure"] = unit_of_measure
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


def log_status_notification(status: str, request_id: int | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"status": status}
    if request_id is not None:
        payload["requestId"] = request_id
    return payload


def firmware_status_notification(status: str, request_id: int | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"status": status}
    if request_id is not None:
        payload["requestId"] = request_id
    return payload


def sign_certificate(csr: str, certificate_type: str = "ChargingStationCertificate") -> dict[str, Any]:
    return {"csr": csr, "certificateType": certificate_type}


def data_transfer(vendor_id: str, message_id: str = "", data: Any = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"vendorId": vendor_id}
    if message_id:
        payload["messageId"] = message_id
    if data is not None:
        payload["data"] = data
    return payload


# ---------------------------------------------------------------------------
# Response parsers (CSMS → CS)
# ---------------------------------------------------------------------------

class BootNotificationResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.status: str = payload.get("status", "Rejected")
        self.current_time: str = payload.get("currentTime", _now_iso())
        self.interval: int = payload.get("interval", 60)
        self.status_info: dict[str, Any] = payload.get("statusInfo", {})

    @property
    def accepted(self) -> bool:
        return self.status == "Accepted"


class AuthorizeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        id_token_info = payload.get("idTokenInfo", {})
        self.status: str = id_token_info.get("status", "Invalid")
        self.evse_id: list[int] = id_token_info.get("evseId", [])

    @property
    def accepted(self) -> bool:
        return self.status == "Accepted"


class TransactionEventResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.total_cost: float | None = payload.get("totalCost")
        self.charging_priority: int | None = payload.get("chargingPriority")
        id_token_info = payload.get("idTokenInfo", {})
        self.id_token_status: str = id_token_info.get("status", "")


# ---------------------------------------------------------------------------
# CSMS → CS action payloads (for parsing inbound handlers)
# ---------------------------------------------------------------------------

class RequestStartTransactionRequest:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.request_id: int = payload.get("requestId", 0)
        evse = payload.get("evse", {})
        self.evse_id: int | None = evse.get("id")
        self.connector_id: int | None = evse.get("connectorId")
        id_token = payload.get("idToken", {})
        self.id_token: str = id_token.get("idToken", "")
        self.id_token_type: str = id_token.get("type", "Central")
        self.remote_start_id: int = payload.get("remoteStartId", 0)


class RequestStopTransactionRequest:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.transaction_id: str = payload.get("transactionId", "")


class ChangeAvailabilityRequest:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.operational_status: str = payload.get("operationalStatus", "Operative")
        evse = payload.get("evse", {})
        self.evse_id: int | None = evse.get("id")
        self.connector_id: int | None = evse.get("connectorId")


class SetVariablesRequest:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.set_variable_data: list[dict[str, Any]] = payload.get("setVariableData", [])


class GetVariablesRequest:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.get_variable_data: list[dict[str, Any]] = payload.get("getVariableData", [])


class ResetRequest:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.reset_type: str = payload.get("type", "Immediate")  # Immediate | OnIdle
        self.evse_id: int | None = payload.get("evseId")
