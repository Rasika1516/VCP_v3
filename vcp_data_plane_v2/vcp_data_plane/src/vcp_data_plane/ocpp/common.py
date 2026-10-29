"""OCPP shared types, message framing and error codes.

Both OCPP 1.6J and 2.0.1 use the same JSON/WebSocket framing:
  CALL        [2, "<MessageId>", "<Action>", {payload}]
  CALLRESULT  [3, "<MessageId>", {payload}]
  CALLERROR   [4, "<MessageId>", "<ErrorCode>", "<Description>", {details}]
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any


class MessageType(IntEnum):
    CALL = 2
    CALLRESULT = 3
    CALLERROR = 4


# OCPP error codes (shared across 1.6J and 2.0.1)
class ErrorCode(str):
    NOT_IMPLEMENTED = "NotImplemented"
    NOT_SUPPORTED = "NotSupported"
    INTERNAL_ERROR = "InternalError"
    PROTOCOL_ERROR = "ProtocolError"
    SECURITY_ERROR = "SecurityError"
    FORMATION_VIOLATION = "FormationViolation"
    PROPERTY_CONSTRAINT_VIOLATION = "PropertyConstraintViolation"
    OCCURRENCE_CONSTRAINT_VIOLATION = "OccurrenceConstraintViolation"
    TYPE_CONSTRAINT_VIOLATION = "TypeConstraintViolation"
    GENERIC_ERROR = "GenericError"


@dataclass
class OcppCall:
    message_id: str
    action: str
    payload: dict[str, Any]

    @classmethod
    def new(cls, action: str, payload: dict[str, Any]) -> "OcppCall":
        return cls(message_id=str(uuid.uuid4()), action=action, payload=payload)

    def to_json(self) -> str:
        return json.dumps([MessageType.CALL, self.message_id, self.action, self.payload])


@dataclass
class OcppCallResult:
    message_id: str
    payload: dict[str, Any]

    def to_json(self) -> str:
        return json.dumps([MessageType.CALLRESULT, self.message_id, self.payload])


@dataclass
class OcppCallError:
    message_id: str
    error_code: str
    error_description: str = ""
    error_details: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(
            [MessageType.CALLERROR, self.message_id, self.error_code, self.error_description, self.error_details]
        )


def parse_message(raw: str) -> OcppCall | OcppCallResult | OcppCallError:
    """Parse a raw OCPP JSON WebSocket message into a typed object.

    Raises ValueError on malformed frames.
    """
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON: {exc}") from exc

    if not isinstance(msg, list) or len(msg) < 3:
        raise ValueError("OCPP message must be a JSON array of ≥ 3 elements")

    msg_type = msg[0]

    if msg_type == MessageType.CALL:
        if len(msg) != 4:
            raise ValueError("CALL must have 4 elements")
        return OcppCall(message_id=str(msg[1]), action=str(msg[2]), payload=msg[3] or {})

    if msg_type == MessageType.CALLRESULT:
        if len(msg) != 3:
            raise ValueError("CALLRESULT must have 3 elements")
        return OcppCallResult(message_id=str(msg[1]), payload=msg[2] or {})

    if msg_type == MessageType.CALLERROR:
        if len(msg) < 4:
            raise ValueError("CALLERROR must have ≥ 4 elements")
        details = msg[4] if len(msg) > 4 else {}
        return OcppCallError(
            message_id=str(msg[1]),
            error_code=str(msg[2]),
            error_description=str(msg[3]),
            error_details=details or {},
        )

    raise ValueError(f"unknown OCPP message type: {msg_type}")


class PendingRequest:
    """Tracks an in-flight CALL waiting for a CALLRESULT or CALLERROR."""

    def __init__(self, call: OcppCall) -> None:
        import asyncio
        self.call = call
        self.future: asyncio.Future[OcppCallResult | OcppCallError] = asyncio.get_event_loop().create_future()

    def resolve(self, result: OcppCallResult | OcppCallError) -> None:
        if not self.future.done():
            self.future.set_result(result)

    def cancel(self, reason: str = "request cancelled") -> None:
        if not self.future.done():
            self.future.cancel()
