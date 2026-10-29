"""OCPP 1.6J protocol adapter.

The adapter is the glue between the VCP runtime (which speaks in terms of
charger events — "boot", "plug EV", "start charge") and the OCPP 1.6J wire
protocol (which speaks in terms of JSON messages).

Usage pattern:
    adapter = V16Adapter(vcp_id, router, profile)
    await adapter.send_boot_notification()
    await adapter.send_status_notification(connector_id=1, status="Available")
    await adapter.send_start_transaction(...)
"""
from __future__ import annotations

import logging
from typing import Any

from vcp_data_plane.config.schema import ChargerProfile
from vcp_data_plane.ocpp.common import OcppCallError, OcppCallResult
from vcp_data_plane.ocpp.router import OcppRouter
from vcp_data_plane.ocpp.v16 import messages as M

logger = logging.getLogger(__name__)


class V16Adapter:
    """Sends OCPP 1.6J messages on behalf of one VCP."""

    def __init__(self, vcp_id: str, charge_point_id: str, router: OcppRouter, profile: ChargerProfile) -> None:
        self._vcp_id = vcp_id
        self._cp_id = charge_point_id
        self._router = router
        self._profile = profile

    # ------------------------------------------------------------------
    # Charge Point → CSMS outgoing messages
    # ------------------------------------------------------------------

    async def send_boot_notification(self) -> M.BootNotificationResponse:
        p = self._profile
        payload = M.boot_notification(
            vendor=p.vendor,
            model=p.model,
            firmware=p.firmware,
            serial=f"{p.serial_number_prefix}{self._cp_id}",
        )
        result = await self._router.call("BootNotification", payload)
        if isinstance(result, OcppCallError):
            logger.error("[%s] BootNotification error: %s", self._vcp_id, result.error_description)
            return M.BootNotificationResponse({"status": "Rejected", "currentTime": M._now_iso(), "interval": 60})
        return M.BootNotificationResponse(result.payload)

    async def send_heartbeat(self) -> str:
        """Returns the CSMS current time string."""
        result = await self._router.call("Heartbeat", M.heartbeat())
        if isinstance(result, OcppCallResult):
            return result.payload.get("currentTime", M._now_iso())
        return M._now_iso()

    async def send_status_notification(
        self,
        connector_id: int,
        status: str,
        error_code: str = "NoError",
        info: str = "",
    ) -> None:
        payload = M.status_notification(
            connector_id=connector_id,
            status=status,
            error_code=error_code,
            info=info,
        )
        await self._router.call("StatusNotification", payload)

    async def send_authorize(self, id_tag: str) -> M.AuthorizeResponse:
        result = await self._router.call("Authorize", M.authorize(id_tag))
        if isinstance(result, OcppCallError):
            logger.warning("[%s] Authorize error: %s", self._vcp_id, result.error_description)
            return M.AuthorizeResponse({"idTagInfo": {"status": "Invalid"}})
        return M.AuthorizeResponse(result.payload)

    async def send_start_transaction(
        self, connector_id: int, id_tag: str, meter_start: int
    ) -> M.StartTransactionResponse:
        payload = M.start_transaction(
            connector_id=connector_id,
            id_tag=id_tag,
            meter_start=meter_start,
        )
        result = await self._router.call("StartTransaction", payload)
        if isinstance(result, OcppCallError):
            logger.error("[%s] StartTransaction error: %s", self._vcp_id, result.error_description)
            return M.StartTransactionResponse({"transactionId": 0, "idTagInfo": {"status": "Invalid"}})
        return M.StartTransactionResponse(result.payload)

    async def send_stop_transaction(
        self,
        transaction_id: int,
        meter_stop: int,
        reason: str = "Local",
        id_tag: str | None = None,
    ) -> None:
        payload = M.stop_transaction(
            transaction_id=transaction_id,
            meter_stop=meter_stop,
            reason=reason,
            id_tag=id_tag,
        )
        await self._router.call("StopTransaction", payload)

    async def send_meter_values(
        self,
        connector_id: int,
        sampled_values: list[dict[str, Any]],
        transaction_id: int | None = None,
    ) -> None:
        entry = M.build_meter_value_entry(sampled_values)
        payload = M.meter_values(connector_id, [entry], transaction_id)
        await self._router.call("MeterValues", payload)

    async def send_data_transfer(self, vendor_id: str, message_id: str = "", data: str = "") -> dict[str, Any]:
        payload = M.data_transfer(vendor_id, message_id, data)
        result = await self._router.call("DataTransfer", payload)
        if isinstance(result, OcppCallResult):
            return result.payload
        return {"status": "Rejected"}

    async def send_diagnostics_status(self, status: str) -> None:
        await self._router.call("DiagnosticsStatusNotification", M.diagnostics_status_notification(status))

    async def send_firmware_status(self, status: str) -> None:
        await self._router.call("FirmwareStatusNotification", M.firmware_status_notification(status))
