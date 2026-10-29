"""Minimal mock CSMS for local runs, demos and integration tests.

Accepts OCPP 1.6J and 2.0.1 WebSocket connections at ``ws://host:port/<cp_id>``
and answers every charge-point request with a valid "Accepted"-style
response.  It can also push CSMS→CP calls (RemoteStart/Stop, Reset, ...)
through ``MockCsms.call()``.

Not a conformance tool — it validates nothing.  Use it to exercise the
simulator end-to-end without a real CSMS.

Run standalone:
    python -m vcp_data_plane.devtools.mock_csms --port 9000
"""
from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

import websockets

logger = logging.getLogger("mock_csms")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class MockCsms:
    def __init__(self, host: str = "127.0.0.1", port: int = 0, heartbeat_interval: int = 300) -> None:
        self.host = host
        self.port = port
        self.heartbeat_interval = heartbeat_interval
        self.connections: dict[str, Any] = {}
        self.received: list[tuple[str, str, dict[str, Any]]] = []   # (cp_id, action, payload)
        self._server: Any = None
        self._txn = itertools.count(1)
        self._pending: dict[str, asyncio.Future] = {}

    # ------------------------------------------------------------------
    async def start(self) -> "MockCsms":
        self._server = await websockets.serve(
            self._handler, self.host, self.port, subprotocols=["ocpp1.6", "ocpp2.0.1"],
        )
        self.port = self._server.sockets[0].getsockname()[1]
        logger.info("Mock CSMS listening on ws://%s:%d", self.host, self.port)
        return self

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    @property
    def url(self) -> str:
        return f"ws://{self.host}:{self.port}"

    def actions(self, cp_id: str | None = None) -> list[str]:
        return [a for c, a, _ in self.received if cp_id is None or c == cp_id]

    async def call(self, cp_id: str, action: str, payload: dict[str, Any], timeout: float = 10) -> Any:
        """Send a CSMS→CP CALL and wait for the CALLRESULT payload."""
        ws = self.connections[cp_id]
        mid = str(uuid.uuid4())
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        await ws.send(json.dumps([2, mid, action, payload]))
        return await asyncio.wait_for(fut, timeout)

    # ------------------------------------------------------------------
    async def _handler(self, ws: Any) -> None:
        path = getattr(getattr(ws, "request", None), "path", None) or getattr(ws, "path", "/")
        cp_id = path.rstrip("/").rsplit("/", 1)[-1] or "unknown"
        self.connections[cp_id] = ws
        logger.info("CP connected: %s (%s)", cp_id, ws.subprotocol)
        try:
            async for raw in ws:
                msg = json.loads(raw)
                if msg[0] == 2:
                    _, mid, action, payload = msg
                    self.received.append((cp_id, action, payload))
                    await ws.send(json.dumps([3, mid, self._respond(action, payload, ws.subprotocol)]))
                elif msg[0] in (3, 4):
                    fut = self._pending.pop(msg[1], None)
                    if fut and not fut.done():
                        fut.set_result(msg[2] if msg[0] == 3 else {"error": msg[2:]})
        except websockets.ConnectionClosed:
            pass
        finally:
            if self.connections.get(cp_id) is ws:
                del self.connections[cp_id]
            logger.info("CP disconnected: %s", cp_id)

    def _respond(self, action: str, payload: dict[str, Any], subprotocol: str | None) -> dict[str, Any]:
        v201 = subprotocol == "ocpp2.0.1"
        if action == "BootNotification":
            return {"status": "Accepted", "currentTime": _now(), "interval": self.heartbeat_interval}
        if action == "Heartbeat":
            return {"currentTime": _now()}
        if action == "Authorize":
            return {"idTokenInfo": {"status": "Accepted"}} if v201 else {"idTagInfo": {"status": "Accepted"}}
        if action == "StartTransaction":
            return {"transactionId": next(self._txn), "idTagInfo": {"status": "Accepted"}}
        if action == "StopTransaction":
            return {"idTagInfo": {"status": "Accepted"}}
        if action == "TransactionEvent":
            return {"idTokenInfo": {"status": "Accepted"}} if payload.get("idToken") else {}
        return {}


def main() -> None:
    ap = argparse.ArgumentParser(description="Mock OCPP CSMS")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--heartbeat-interval", type=int, default=300)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")

    async def _run() -> None:
        csms = await MockCsms(args.host, args.port, args.heartbeat_interval).start()
        print(f"Mock CSMS ready on ws://{args.host}:{csms.port}/<charge_point_id>", flush=True)
        await asyncio.Future()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
