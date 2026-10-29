"""OCPP 2.0.1 inbound CSMS→CS action handlers."""
from __future__ import annotations

import logging
from typing import Any, Callable, Coroutine

from vcp_data_plane.ocpp.common import OcppCall

logger = logging.getLogger(__name__)

OnRemoteStart = Callable[[int | None, int | None, str, str, int], Coroutine[Any, Any, str]]
OnRemoteStop = Callable[[str], Coroutine[Any, Any, str]]
OnReset = Callable[[str, int | None], Coroutine[Any, Any, None]]
OnSetVariables = Callable[[list[dict[str, Any]]], Coroutine[Any, Any, list[dict[str, Any]]]]
OnGetVariables = Callable[[list[dict[str, Any]]], Coroutine[Any, Any, list[dict[str, Any]]]]
OnChangeAvailability = Callable[[int | None, int | None, str], Coroutine[Any, Any, str]]
OnTrigger = Callable[[str, int | None], Coroutine[Any, Any, str]]
OnUnlock = Callable[[int, int], Coroutine[Any, Any, str]]


class V201Handlers:
    def __init__(
        self,
        vcp_id: str,
        on_remote_start: OnRemoteStart,
        on_remote_stop: OnRemoteStop,
        on_reset: OnReset,
        on_set_variables: OnSetVariables,
        on_get_variables: OnGetVariables,
        on_change_availability: OnChangeAvailability,
        on_trigger: OnTrigger,
        on_unlock: OnUnlock,
    ) -> None:
        self._vcp_id = vcp_id
        self._on_remote_start = on_remote_start
        self._on_remote_stop = on_remote_stop
        self._on_reset = on_reset
        self._on_set_variables = on_set_variables
        self._on_get_variables = on_get_variables
        self._on_change_availability = on_change_availability
        self._on_trigger = on_trigger
        self._on_unlock = on_unlock

    async def handle_request_start_transaction(self, call: OcppCall) -> dict[str, Any]:
        evse = call.payload.get("evse", {})
        evse_id = evse.get("id")
        connector_id = evse.get("connectorId")
        id_token_obj = call.payload.get("idToken", {})
        id_token = id_token_obj.get("idToken", "")
        id_token_type = id_token_obj.get("type", "Central")
        request_id = call.payload.get("requestId", 0)
        remote_start_id = call.payload.get("remoteStartId", 0)
        logger.info("[%s] CSMS RequestStartTransaction evse=%s token=%s", self._vcp_id, evse_id, id_token)
        status = await self._on_remote_start(evse_id, connector_id, id_token, id_token_type, remote_start_id)
        return {"status": status}  # Accepted | Rejected

    async def handle_request_stop_transaction(self, call: OcppCall) -> dict[str, Any]:
        transaction_id = call.payload.get("transactionId", "")
        logger.info("[%s] CSMS RequestStopTransaction txn=%s", self._vcp_id, transaction_id)
        status = await self._on_remote_stop(transaction_id)
        return {"status": status}

    async def handle_change_availability(self, call: OcppCall) -> dict[str, Any]:
        operational_status = call.payload.get("operationalStatus", "Operative")
        evse = call.payload.get("evse", {})
        evse_id = evse.get("id")
        connector_id = evse.get("connectorId")
        logger.info("[%s] CSMS ChangeAvailability status=%s evse=%s", self._vcp_id, operational_status, evse_id)
        status = await self._on_change_availability(evse_id, connector_id, operational_status)
        return {"status": status}

    async def handle_reset(self, call: OcppCall) -> dict[str, Any]:
        reset_type = call.payload.get("type", "Immediate")
        evse_id = call.payload.get("evseId")
        logger.info("[%s] CSMS Reset type=%s evse=%s", self._vcp_id, reset_type, evse_id)
        await self._on_reset(reset_type, evse_id)
        return {"status": "Accepted"}

    async def handle_set_variables(self, call: OcppCall) -> dict[str, Any]:
        variable_data = call.payload.get("setVariableData", [])
        logger.debug("[%s] CSMS SetVariables count=%d", self._vcp_id, len(variable_data))
        results = await self._on_set_variables(variable_data)
        return {"setVariableResult": results}

    async def handle_get_variables(self, call: OcppCall) -> dict[str, Any]:
        variable_data = call.payload.get("getVariableData", [])
        logger.debug("[%s] CSMS GetVariables count=%d", self._vcp_id, len(variable_data))
        results = await self._on_get_variables(variable_data)
        return {"getVariableResult": results}

    async def handle_clear_cache(self, call: OcppCall) -> dict[str, Any]:
        logger.info("[%s] CSMS ClearCache", self._vcp_id)
        return {"status": "Accepted"}

    async def handle_trigger_message(self, call: OcppCall) -> dict[str, Any]:
        requested_message = call.payload.get("requestedMessage", "")
        evse = call.payload.get("evse", {})
        evse_id = evse.get("id")
        logger.info("[%s] CSMS TriggerMessage msg=%s evse=%s", self._vcp_id, requested_message, evse_id)
        status = await self._on_trigger(requested_message, evse_id)
        return {"status": status}

    async def handle_unlock_connector(self, call: OcppCall) -> dict[str, Any]:
        evse_id = call.payload.get("evseId", 1)
        connector_id = call.payload.get("connectorId", 1)
        logger.info("[%s] CSMS UnlockConnector evse=%s connector=%s", self._vcp_id, evse_id, connector_id)
        status = await self._on_unlock(evse_id, connector_id)
        return {"status": status}

    async def handle_data_transfer(self, call: OcppCall) -> dict[str, Any]:
        return {"status": "UnknownVendorId"}

    async def handle_get_log(self, call: OcppCall) -> dict[str, Any]:
        return {"status": "Rejected"}

    async def handle_update_firmware(self, call: OcppCall) -> dict[str, Any]:
        return {"status": "Rejected"}

    async def handle_get_transaction_status(self, call: OcppCall) -> dict[str, Any]:
        return {"messagesInQueue": False, "ongoingIndicator": False}

    async def handle_send_local_list(self, call: OcppCall) -> dict[str, Any]:
        return {"status": "Failed"}

    async def handle_reserve_now(self, call: OcppCall) -> dict[str, Any]:
        return {"status": "Rejected"}

    async def handle_cancel_reservation(self, call: OcppCall) -> dict[str, Any]:
        return {"status": "Rejected"}

    async def handle_set_charging_profile(self, call: OcppCall) -> dict[str, Any]:
        return {"status": "Accepted"}

    async def handle_clear_charging_profile(self, call: OcppCall) -> dict[str, Any]:
        return {"status": "Accepted"}

    async def handle_get_composite_schedule(self, call: OcppCall) -> dict[str, Any]:
        return {"status": "Rejected"}

    def register_all(self, router: Any) -> None:
        router.register("RequestStartTransaction", self.handle_request_start_transaction)
        router.register("RequestStopTransaction", self.handle_request_stop_transaction)
        router.register("ChangeAvailability", self.handle_change_availability)
        router.register("Reset", self.handle_reset)
        router.register("SetVariables", self.handle_set_variables)
        router.register("GetVariables", self.handle_get_variables)
        router.register("ClearCache", self.handle_clear_cache)
        router.register("TriggerMessage", self.handle_trigger_message)
        router.register("UnlockConnector", self.handle_unlock_connector)
        router.register("DataTransfer", self.handle_data_transfer)
        router.register("GetLog", self.handle_get_log)
        router.register("UpdateFirmware", self.handle_update_firmware)
        router.register("GetTransactionStatus", self.handle_get_transaction_status)
        router.register("SendLocalList", self.handle_send_local_list)
        router.register("ReserveNow", self.handle_reserve_now)
        router.register("CancelReservation", self.handle_cancel_reservation)
        router.register("SetChargingProfile", self.handle_set_charging_profile)
        router.register("ClearChargingProfile", self.handle_clear_charging_profile)
        router.register("GetCompositeSchedule", self.handle_get_composite_schedule)
