"""OCPP 1.6J inbound CSMS→CP action handlers.

Each handler is an async coroutine that receives an OcppCall and returns the
response payload dict.  Handlers are registered on the OcppRouter and are
invoked when the CSMS sends a CALL to the VCP.

Handlers here do the minimum protocol work (validate, ack) and delegate
behavioral side-effects to the BehaviorEngine via the callback hooks passed
at construction.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Coroutine

from vcp_data_plane.ocpp.common import OcppCall

logger = logging.getLogger(__name__)


# Callback type signatures
OnRemoteStart = Callable[[int | None, str], Coroutine[Any, Any, bool]]
OnRemoteStop = Callable[[int], Coroutine[Any, Any, bool]]
OnReset = Callable[[str], Coroutine[Any, Any, None]]
OnUnlock = Callable[[int], Coroutine[Any, Any, str]]
OnGetConfig = Callable[[list[str]], Coroutine[Any, Any, dict[str, Any]]]
OnChangeConfig = Callable[[str, str], Coroutine[Any, Any, str]]
OnTrigger = Callable[[str, int | None], Coroutine[Any, Any, str]]
OnChangeAvailability = Callable[[int, str], Coroutine[Any, Any, str]]


class V16Handlers:
    """Encapsulates all CSMS→CP OCPP 1.6J handlers for one VCP instance.

    Hooks are provided as callables by the VCP runtime so that the behavior
    engine drives the side-effects while the handler code stays thin.
    """

    def __init__(
        self,
        vcp_id: str,
        on_remote_start: OnRemoteStart,
        on_remote_stop: OnRemoteStop,
        on_reset: OnReset,
        on_unlock: OnUnlock,
        on_get_config: OnGetConfig,
        on_change_config: OnChangeConfig,
        on_trigger: OnTrigger,
        on_change_availability: OnChangeAvailability,
    ) -> None:
        self._vcp_id = vcp_id
        self._on_remote_start = on_remote_start
        self._on_remote_stop = on_remote_stop
        self._on_reset = on_reset
        self._on_unlock = on_unlock
        self._on_get_config = on_get_config
        self._on_change_config = on_change_config
        self._on_trigger = on_trigger
        self._on_change_availability = on_change_availability

    async def handle_remote_start_transaction(self, call: OcppCall) -> dict[str, Any]:
        connector_id = call.payload.get("connectorId")
        id_tag = call.payload.get("idTag", "UNKNOWN")
        logger.info("[%s] CSMS RemoteStartTransaction connector=%s tag=%s", self._vcp_id, connector_id, id_tag)
        accepted = await self._on_remote_start(connector_id, id_tag)
        return {"status": "Accepted" if accepted else "Rejected"}

    async def handle_remote_stop_transaction(self, call: OcppCall) -> dict[str, Any]:
        transaction_id = call.payload.get("transactionId", 0)
        logger.info("[%s] CSMS RemoteStopTransaction txnId=%s", self._vcp_id, transaction_id)
        accepted = await self._on_remote_stop(transaction_id)
        return {"status": "Accepted" if accepted else "Rejected"}

    async def handle_change_availability(self, call: OcppCall) -> dict[str, Any]:
        connector_id = call.payload.get("connectorId", 0)
        availability_type = call.payload.get("type", "Operative")
        logger.info("[%s] CSMS ChangeAvailability connector=%s type=%s", self._vcp_id, connector_id, availability_type)
        status = await self._on_change_availability(connector_id, availability_type)
        return {"status": status}  # Accepted | Rejected | Scheduled

    async def handle_change_configuration(self, call: OcppCall) -> dict[str, Any]:
        key = call.payload.get("key", "")
        value = call.payload.get("value", "")
        logger.info("[%s] CSMS ChangeConfiguration key=%s value=%s", self._vcp_id, key, value)
        status = await self._on_change_config(key, value)
        return {"status": status}  # Accepted | Rejected | RebootRequired | NotSupported

    async def handle_get_configuration(self, call: OcppCall) -> dict[str, Any]:
        keys: list[str] = call.payload.get("key", [])
        logger.debug("[%s] CSMS GetConfiguration keys=%s", self._vcp_id, keys)
        config = await self._on_get_config(keys)
        # config_key entries: {"key": str, "readonly": bool, "value": str}
        return {
            "configurationKey": config.get("known", []),
            "unknownKey": config.get("unknown", []),
        }

    async def handle_clear_cache(self, call: OcppCall) -> dict[str, Any]:
        logger.info("[%s] CSMS ClearCache", self._vcp_id)
        return {"status": "Accepted"}

    async def handle_reset(self, call: OcppCall) -> dict[str, Any]:
        reset_type = call.payload.get("type", "Soft")
        logger.info("[%s] CSMS Reset type=%s", self._vcp_id, reset_type)
        await self._on_reset(reset_type)
        return {"status": "Accepted"}

    async def handle_unlock_connector(self, call: OcppCall) -> dict[str, Any]:
        connector_id = call.payload.get("connectorId", 1)
        logger.info("[%s] CSMS UnlockConnector connector=%s", self._vcp_id, connector_id)
        status = await self._on_unlock(connector_id)
        return {"status": status}  # Unlocked | UnlockFailed | NotSupported

    async def handle_trigger_message(self, call: OcppCall) -> dict[str, Any]:
        requested_message = call.payload.get("requestedMessage", "")
        connector_id = call.payload.get("connectorId")
        logger.info("[%s] CSMS TriggerMessage msg=%s connector=%s", self._vcp_id, requested_message, connector_id)
        status = await self._on_trigger(requested_message, connector_id)
        return {"status": status}  # Accepted | Rejected | NotImplemented

    async def handle_data_transfer(self, call: OcppCall) -> dict[str, Any]:
        logger.debug("[%s] CSMS DataTransfer vendorId=%s", self._vcp_id, call.payload.get("vendorId"))
        return {"status": "UnknownVendorId"}

    async def handle_get_diagnostics(self, call: OcppCall) -> dict[str, Any]:
        logger.info("[%s] CSMS GetDiagnostics", self._vcp_id)
        return {}  # No filename — simulator doesn't generate real diagnostics

    async def handle_update_firmware(self, call: OcppCall) -> dict[str, Any]:
        logger.info("[%s] CSMS UpdateFirmware location=%s", self._vcp_id, call.payload.get("location"))
        return {}

    async def handle_send_local_list(self, call: OcppCall) -> dict[str, Any]:
        logger.info("[%s] CSMS SendLocalList", self._vcp_id)
        return {"status": "NotSupported"}

    async def handle_get_local_list_version(self, call: OcppCall) -> dict[str, Any]:
        return {"listVersion": 0}

    async def handle_reserve_now(self, call: OcppCall) -> dict[str, Any]:
        logger.info("[%s] CSMS ReserveNow connectorId=%s", self._vcp_id, call.payload.get("connectorId"))
        return {"status": "Rejected"}

    async def handle_cancel_reservation(self, call: OcppCall) -> dict[str, Any]:
        return {"status": "Rejected"}

    async def handle_set_charging_profile(self, call: OcppCall) -> dict[str, Any]:
        logger.info("[%s] CSMS SetChargingProfile", self._vcp_id)
        return {"status": "Accepted"}

    async def handle_clear_charging_profile(self, call: OcppCall) -> dict[str, Any]:
        return {"status": "Accepted"}

    async def handle_get_composite_schedule(self, call: OcppCall) -> dict[str, Any]:
        return {"status": "Rejected"}

    def register_all(self, router: Any) -> None:
        """Register all handlers on an OcppRouter instance."""
        router.register("RemoteStartTransaction", self.handle_remote_start_transaction)
        router.register("RemoteStopTransaction", self.handle_remote_stop_transaction)
        router.register("ChangeAvailability", self.handle_change_availability)
        router.register("ChangeConfiguration", self.handle_change_configuration)
        router.register("GetConfiguration", self.handle_get_configuration)
        router.register("ClearCache", self.handle_clear_cache)
        router.register("Reset", self.handle_reset)
        router.register("UnlockConnector", self.handle_unlock_connector)
        router.register("TriggerMessage", self.handle_trigger_message)
        router.register("DataTransfer", self.handle_data_transfer)
        router.register("GetDiagnostics", self.handle_get_diagnostics)
        router.register("UpdateFirmware", self.handle_update_firmware)
        router.register("SendLocalList", self.handle_send_local_list)
        router.register("GetLocalListVersion", self.handle_get_local_list_version)
        router.register("ReserveNow", self.handle_reserve_now)
        router.register("CancelReservation", self.handle_cancel_reservation)
        router.register("SetChargingProfile", self.handle_set_charging_profile)
        router.register("ClearChargingProfile", self.handle_clear_charging_profile)
        router.register("GetCompositeSchedule", self.handle_get_composite_schedule)
