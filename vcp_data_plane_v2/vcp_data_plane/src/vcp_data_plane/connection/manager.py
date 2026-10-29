"""WebSocket Connection Manager.

Manages the full WebSocket lifecycle for one VCP:
  - Initial connect with boot jitter
  - WebSocket open → run receive loop → pass raw messages to OcppRouter
  - Reconnect with exponential backoff on disconnect
  - Graceful close on shutdown
  - Expose a `send(raw_str)` coroutine for the adapter/router to call

The connection manager owns the WebSocket object; everything above it
(router, adapter, behavior engine) is wire-agnostic.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Coroutine

import websockets
from websockets.asyncio.client import ClientConnection

from vcp_data_plane.behavior.timer import boot_jitter_sleep, reconnect_backoff
from vcp_data_plane.config.schema import CsmsConfig, OcppVersion, SecurityProfile
from vcp_data_plane.connection.transport import build_ssl_context, websocket_subprotocol, websocket_url
from vcp_data_plane.state.machine import VcpState, VcpStateMachine

logger = logging.getLogger(__name__)

# Called on each received raw message
MessageCallback = Callable[[str], Coroutine[Any, Any, None]]
# Called on connect and disconnect events
EventCallback = Callable[[str], Coroutine[Any, Any, None]]


class ConnectionManager:
    """Manages WebSocket connection for a single VCP."""

    def __init__(
        self,
        vcp_id: str,
        charge_point_id: str,
        csms_config: CsmsConfig,
        ocpp_version: OcppVersion,
        security_profile: SecurityProfile,
        state_machine: VcpStateMachine,
        on_message: MessageCallback,
        on_connected: EventCallback | None = None,
        on_disconnected: EventCallback | None = None,
        boot_jitter_ms: tuple[int, int] = (0, 0),
    ) -> None:
        self._vcp_id = vcp_id
        self._cp_id = charge_point_id
        self._csms = csms_config
        self._ocpp_version = ocpp_version
        self._security_profile = security_profile
        self._state = state_machine
        self._on_message = on_message
        self._on_connected = on_connected
        self._on_disconnected = on_disconnected
        self._boot_jitter_ms = boot_jitter_ms

        self._ws: ClientConnection | None = None
        self._running = False
        self._reconnect_attempt = 0
        self._connect_task: asyncio.Task[None] | None = None

        # Pre-build TLS context (once, reused across reconnects)
        self._ssl_ctx = build_ssl_context(self._csms.auth, self._security_profile)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the connection loop (non-blocking — runs in background task)."""
        self._running = True
        await boot_jitter_sleep(self._boot_jitter_ms)
        self._connect_task = asyncio.create_task(self._connection_loop(), name=f"{self._vcp_id}:ws")

    async def stop(self) -> None:
        """Gracefully close the connection and stop reconnecting."""
        self._running = False
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
        if self._connect_task and not self._connect_task.done():
            self._connect_task.cancel()
            try:
                await self._connect_task
            except asyncio.CancelledError:
                pass

    async def send(self, raw: str) -> None:
        """Send a raw OCPP JSON string to the CSMS."""
        if self._ws is None:
            raise ConnectionError(f"[{self._vcp_id}] WebSocket not connected")
        try:
            await self._ws.send(raw)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[%s] Send failed: %s", self._vcp_id, exc)
            raise

    @property
    def is_connected(self) -> bool:
        return self._ws is not None

    # ------------------------------------------------------------------
    # Internal connection loop
    # ------------------------------------------------------------------

    async def _connection_loop(self) -> None:
        """Connect, receive, reconnect with backoff."""
        while self._running:
            try:
                await self._connect_and_receive()
                self._reconnect_attempt = 0  # Reset on clean disconnect
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[%s] Connection error (attempt %d): %s",
                    self._vcp_id, self._reconnect_attempt, exc,
                )

            if not self._running:
                break

            backoff = reconnect_backoff(
                attempt=self._reconnect_attempt,
                initial_s=self._csms.initial_backoff_s,
                multiplier=self._csms.backoff_multiplier,
                max_s=self._csms.max_backoff_s,
            )
            self._reconnect_attempt += 1
            logger.info("[%s] Reconnecting in %.1fs (attempt %d)", self._vcp_id, backoff, self._reconnect_attempt)
            await self._state.set_connection_state(VcpState.OFFLINE, "disconnected")
            await asyncio.sleep(backoff)
            await self._state.set_connection_state(VcpState.RECOVERING, "reconnecting")

    async def _connect_and_receive(self) -> None:
        url = websocket_url(self._csms.url, self._cp_id)
        subprotocols = websocket_subprotocol(self._ocpp_version.value)
        headers = self._build_auth_headers()

        logger.info("[%s] Connecting to %s", self._vcp_id, url)

        async with websockets.connect(
            url,
            subprotocols=subprotocols,
            additional_headers=headers,
            # websockets rejects an SSL context for ws:// URLs — only pass it for wss://
            ssl=self._ssl_ctx if url.lower().startswith("wss://") else None,
            open_timeout=self._csms.connect_timeout_s,
            ping_interval=self._csms.ping_interval_s,
            ping_timeout=self._csms.ping_timeout_s,
            max_size=1_048_576,  # 1 MB max message size
        ) as ws:
            self._ws = ws
            logger.info("[%s] Connected", self._vcp_id)
            await self._state.set_connection_state(VcpState.RECOVERING, "connected")

            if self._on_connected:
                await self._on_connected(self._vcp_id)

            try:
                async for raw_msg in ws:
                    if isinstance(raw_msg, bytes):
                        raw_msg = raw_msg.decode("utf-8")
                    await self._on_message(raw_msg)
            finally:
                self._ws = None
                logger.info("[%s] Disconnected", self._vcp_id)
                if self._on_disconnected:
                    await self._on_disconnected(self._vcp_id)

    def _build_auth_headers(self) -> dict[str, str]:
        auth = self._csms.auth
        headers: dict[str, str] = {}
        if auth.type.value == "basic" and auth.username and auth.password:
            import base64
            credentials = base64.b64encode(f"{auth.username}:{auth.password}".encode()).decode()
            headers["Authorization"] = f"Basic {credentials}"
        return headers
