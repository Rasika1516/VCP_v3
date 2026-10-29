"""Worker directory — the control plane's view of data-plane workers.

Workers become known from three sources:

  static      VCP_CP_WORKERS ("id=url,…") or legacy VCP_CP_WORKER_URLS (URL only;
              the worker ID is resolved from the worker's GET /health)
  fleet       fleets deployed from the UI (vcp_fleet table)
  discovered  a vcp:events:worker:{id} stream found in Redis, or a
              worker_status event received from an unknown worker

The EventSubscriber subscribes to ``{event_stream_prefix}:{worker_id}`` for
every known worker, so stream names always match what workers publish to
(fixes the v1 URL-slug mismatch).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

from vcp_control_plane.config.settings import Settings

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class WorkerInfo:
    worker_id: str
    url: str = ""
    source: str = "discovered"          # static | fleet | discovered
    fleet_id: str | None = None
    status: str = "unknown"             # online | heartbeat | offline | unknown
    vcp_count: int = 0
    contract_version: str | None = None
    last_seen: datetime | None = None
    state_distribution: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "workerId": self.worker_id,
            "url": self.url,
            "source": self.source,
            "fleetId": self.fleet_id,
            "status": self.status,
            "vcpCount": self.vcp_count,
            "contractVersion": self.contract_version,
            "lastSeen": self.last_seen.isoformat() if self.last_seen else None,
        }


class WorkerDirectory:
    def __init__(self) -> None:
        self._workers: dict[str, WorkerInfo] = {}
        self._url_ids: dict[str, str] = {}   # legacy URL → resolved worker_id

    # ------------------------------------------------------------------
    def all(self) -> list[WorkerInfo]:
        return sorted(self._workers.values(), key=lambda w: w.worker_id)

    def get(self, worker_id: str) -> WorkerInfo | None:
        return self._workers.get(worker_id)

    def ids(self) -> list[str]:
        return sorted(self._workers)

    def upsert(self, worker_id: str, **fields: Any) -> WorkerInfo:
        info = self._workers.get(worker_id)
        if info is None:
            info = WorkerInfo(worker_id=worker_id)
            self._workers[worker_id] = info
        for k, v in fields.items():
            if v is not None and hasattr(info, k):
                # never downgrade an explicit source to "discovered"
                if k == "source" and v == "discovered" and info.source != "discovered":
                    continue
                setattr(info, k, v)
        return info

    def remove(self, worker_id: str) -> None:
        self._workers.pop(worker_id, None)

    def on_worker_status(self, worker_id: str, payload: dict[str, Any]) -> WorkerInfo:
        status = str(payload.get("status", "online"))
        return self.upsert(
            worker_id,
            status="offline" if status == "offline" else "online",
            vcp_count=int(payload.get("vcp_count", 0) or 0),
            contract_version=payload.get("contract_version"),
            state_distribution=payload.get("state_distribution") or {},
            last_seen=_now(),
        )

    def touch(self, worker_id: str) -> None:
        info = self._workers.get(worker_id)
        if info is not None:
            info.last_seen = _now()
            if info.status in ("unknown", "offline"):
                info.status = "online"

    # ------------------------------------------------------------------
    async def refresh(self, settings: Settings, redis: Any = None, fleets: list[Any] | None = None) -> None:
        """Re-read static config, deployed fleets and Redis streams."""
        for wid, url in settings.static_workers.items():
            self.upsert(wid, url=url, source="static")

        legacy = [u.strip() for u in settings.worker_urls_raw.split(",") if u.strip()]
        for url in legacy:
            wid = self._url_ids.get(url) or await self._resolve_worker_id(url, settings.worker_token)
            if wid:
                self._url_ids[url] = wid
                self.upsert(wid, url=url, source="static")

        if fleets is not None:
            active = set()
            for f in fleets:
                if f.status in ("deploying", "running", "stopping", "failed") or f.worker_url:
                    active.add(f.worker_id)
                    self.upsert(f.worker_id, url=f.worker_url, source="fleet", fleet_id=f.fleet_id)
            # forget fleet workers whose fleet was deleted
            for info in list(self._workers.values()):
                if info.source == "fleet" and info.worker_id not in active:
                    self.remove(info.worker_id)

        if redis is not None and settings.event_stream_discovery:
            prefix = settings.event_stream_prefix + ":"
            try:
                async for key in redis.scan_iter(match=prefix + "*", count=500):
                    key = key.decode() if isinstance(key, bytes) else key
                    wid = key[len(prefix):]
                    if wid and wid != "broadcast" and ":" not in wid:
                        self.upsert(wid, source="discovered")
            except Exception as exc:  # noqa: BLE001
                logger.debug("event stream discovery failed: %s", exc)

    @staticmethod
    async def _resolve_worker_id(url: str, token: str | None) -> str | None:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            async with httpx.AsyncClient(headers=headers, timeout=3.0) as client:
                resp = await client.get(f"{url.rstrip('/')}/health")
                resp.raise_for_status()
                return resp.json().get("worker_id")
        except Exception:  # noqa: BLE001
            logger.debug("could not resolve worker id for %s", url)
            return None

    def event_streams(self, settings: Settings) -> list[str]:
        prefix = settings.event_stream_prefix
        streams = [f"{prefix}:{wid}" for wid in self.ids()]
        streams.append(f"{prefix}:broadcast")
        return streams


_directory = WorkerDirectory()


def get_worker_directory() -> WorkerDirectory:
    return _directory
