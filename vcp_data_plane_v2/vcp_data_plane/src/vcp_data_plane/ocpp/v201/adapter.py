"""OCPP 2.0.1 protocol adapter."""
from __future__ import annotations

import logging
import uuid
from typing import Any

from vcp_data_plane.config.schema import ChargerProfile
from vcp_data_plane.ocpp.common import OcppCallError, OcppCallResult
from vcp_data_plane.ocpp.router import OcppRouter
from vcp_data_plane.ocpp.v201 import messages as M

logger = logging.getLogger(__name__)


class V201Adapter:
    """Sends OCPP 2.0.1 messages on behalf of one VCP (Charging Station)."""

    def __init__(self, vcp_id: str, charge_point_id: str, router: OcppRouter, profile: ChargerProfile) -> None:
        self._vcp_id = vcp_id
        self._cp_id = charge_point_id
        self._router = router
        self._profile = profile
        # Transaction sequence numbers per (evse_id, transaction_id)
        self._seq_nos: dict[str, int] = {}

    def _next_seq(self, transaction_id: str) -> int:
        n = self._seq_nos.get(transaction_id, 0)
        self._seq_nos[transaction_id] = n + 1
        return n

    async def send_boot_notification(self, reason: str = "PowerUp") -> M.BootNotificationResponse:
        p = self._profile
        payload = M.boot_notification(
            reason=reason,
            vendor_name=p.vendor,
            model=p.model,
            firmware_version=p.firmware,
            serial_number=f"{p.serial_number_prefix}{self._cp_id}",
        )
        result = await self._router.call("BootNotification", payload)
        if isinstance(result, OcppCallError):
            logger.error("[%s] BootNotification error: %s", self._vcp_id, result.error_description)
            return M.BootNotificationResponse({"status": "Rejected", "currentTime": M._now_iso(), "interval": 60})
        return M.BootNotificationResponse(result.payload)

    async def send_heartbeat(self) -> str:
        result = await self._router.call("Heartbeat", M.heartbeat())
        if isinstance(result, OcppCallResult):
            return result.payload.get("currentTime", M._now_iso())
        return M._now_iso()

    async def send_status_notification(
        self,
        evse_id: int,
        connector_id: int,
        connector_status: str,
    ) -> None:
        payload = M.status_notification(
            timestamp=M._now_iso(),
            connector_status=connector_status,
            evse_id=evse_id,
            connector_id=connector_id,
        )
        await self._router.call("StatusNotification", payload)

    async def send_authorize(self, id_token: str, id_token_type: str = "Central") -> M.AuthorizeResponse:
        result = await self._router.call("Authorize", M.authorize(id_token, id_token_type))
        if isinstance(result, OcppCallError):
            return M.AuthorizeResponse({"idTokenInfo": {"status": "Invalid"}})
        return M.AuthorizeResponse(result.payload)

    async def send_transaction_event_started(
        self,
        transaction_id: str,
        evse_id: int,
        connector_id: int,
        trigger_reason: str,
        id_token: str | None = None,
        meter_values: list[dict[str, Any]] | None = None,
        remote_start_id: int | None = None,
    ) -> M.TransactionEventResponse:
        payload = M.transaction_event(
            event_type="Started",
            timestamp=M._now_iso(),
            trigger_reason=trigger_reason,
            seq_no=self._next_seq(transaction_id),
            transaction_id=transaction_id,
            evse_id=evse_id,
            connector_id=connector_id,
            id_token=id_token,
            charging_state="EVConnected",
            meter_values=meter_values,
        )
        result = await self._router.call("TransactionEvent", payload)
        if isinstance(result, OcppCallResult):
            return M.TransactionEventResponse(result.payload)
        return M.TransactionEventResponse({})

    async def send_transaction_event_updated(
        self,
        transaction_id: str,
        evse_id: int,
        connector_id: int,
        trigger_reason: str = "MeterValuePeriodic",
        meter_values: list[dict[str, Any]] | None = None,
        charging_state: str = "Charging",
    ) -> M.TransactionEventResponse:
        payload = M.transaction_event(
            event_type="Updated",
            timestamp=M._now_iso(),
            trigger_reason=trigger_reason,
            seq_no=self._next_seq(transaction_id),
            transaction_id=transaction_id,
            evse_id=evse_id,
            connector_id=connector_id,
            meter_values=meter_values,
            charging_state=charging_state,
        )
        result = await self._router.call("TransactionEvent", payload)
        if isinstance(result, OcppCallResult):
            return M.TransactionEventResponse(result.payload)
        return M.TransactionEventResponse({})

    async def send_transaction_event_ended(
        self,
        transaction_id: str,
        evse_id: int,
        connector_id: int,
        trigger_reason: str,
        stopped_reason: str = "Local",
        id_token: str | None = None,
        meter_values: list[dict[str, Any]] | None = None,
    ) -> M.TransactionEventResponse:
        payload = M.transaction_event(
            event_type="Ended",
            timestamp=M._now_iso(),
            trigger_reason=trigger_reason,
            seq_no=self._next_seq(transaction_id),
            transaction_id=transaction_id,
            evse_id=evse_id,
            connector_id=connector_id,
            id_token=id_token,
            charging_state=None,
            stopped_reason=stopped_reason,
            meter_values=meter_values,
        )
        # Clean up sequence counter for this transaction
        self._seq_nos.pop(transaction_id, None)
        result = await self._router.call("TransactionEvent", payload)
        if isinstance(result, OcppCallResult):
            return M.TransactionEventResponse(result.payload)
        return M.TransactionEventResponse({})

    async def send_meter_values(
        self,
        evse_id: int,
        sampled_values: list[dict[str, Any]],
    ) -> None:
        entry = M.build_meter_value_entry(sampled_values)
        payload = M.meter_values(evse_id, [entry])
        await self._router.call("MeterValues", payload)

    async def send_data_transfer(self, vendor_id: str, message_id: str = "", data: Any = None) -> dict[str, Any]:
        result = await self._router.call("DataTransfer", M.data_transfer(vendor_id, message_id, data))
        if isinstance(result, OcppCallResult):
            return result.payload
        return {"status": "Rejected"}

    async def send_firmware_status(self, status: str, request_id: int | None = None) -> None:
        await self._router.call("FirmwareStatusNotification", M.firmware_status_notification(status, request_id))

    async def send_log_status(self, status: str, request_id: int | None = None) -> None:
        await self._router.call("LogStatusNotification", M.log_status_notification(status, request_id))
