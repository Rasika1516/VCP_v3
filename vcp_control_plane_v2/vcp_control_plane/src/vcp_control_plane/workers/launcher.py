"""Worker launchers — start / stop / inspect a fleet's data-plane worker.

Two implementations behind one interface (``WorkerLauncher``):

  ProcessLauncher  runs ``vcp-worker run <worker.yaml>`` as a detached child
                   process on the control-plane host (own session, so the
                   worker survives a control-plane restart).  Output goes to
                   ``{fleet_dir}/worker.log``.
  DockerLauncher   creates one container per fleet from ``settings.docker_image``
                   via the Docker Engine API on ``settings.docker_socket``,
                   bind-mounting the fleet directory at /config.  Networking
                   (VCP_CP_DOCKER_NETWORK):
                     ""        default bridge, worker API/metrics ports published
                               on the host; reach host services via
                               host.docker.internal
                     "host"    host networking (Linux)
                     <name>    a user-defined network shared with the control
                               plane (worker URL = http://vcp-worker-<fleet>:<port>)

A Kubernetes launcher can be added later by implementing the same interface.
"""
from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import shlex
import shutil
import signal
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from vcp_control_plane.config.settings import Settings
from vcp_control_plane.workers.config_gen import fleet_dir

logger = logging.getLogger(__name__)

LAUNCHERS = ("process", "docker")


class LauncherError(Exception):
    pass


@dataclass
class LaunchResult:
    runtime: dict[str, Any]
    worker_url: str
    notes: list[str] = field(default_factory=list)


class WorkerLauncher:
    name = "base"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def uses_container_paths(self) -> bool:
        return False

    async def start(self, fleet: Any, config_path: Path) -> LaunchResult:  # pragma: no cover - interface
        raise NotImplementedError

    async def stop(self, fleet: Any, timeout_s: float = 10.0) -> None:  # pragma: no cover
        raise NotImplementedError

    async def is_alive(self, fleet: Any) -> bool:  # pragma: no cover
        raise NotImplementedError

    async def logs(self, fleet: Any, tail: int = 200) -> str:
        return _tail_file(fleet_dir(self.settings, fleet.fleet_id) / "worker.log", tail)

    async def check(self) -> tuple[bool, str]:
        """Is this launcher usable on this host?  (ok, message)"""
        return True, "ok"


def _tail_file(path: Path, tail: int) -> str:
    if not path.exists():
        return ""
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - 256_000))
        lines = fh.read().decode("utf-8", "replace").splitlines()
    return "\n".join(lines[-tail:])


# ---------------------------------------------------------------------------
# Local process
# ---------------------------------------------------------------------------

class ProcessLauncher(WorkerLauncher):
    name = "process"

    # pid → Popen-like handle for processes started by *this* control-plane
    # process (lets us reap them and read the exit code)
    _children: dict[int, asyncio.subprocess.Process] = {}

    def _command(self) -> list[str]:
        parts = shlex.split(self.settings.worker_command or "vcp-worker")
        if shutil.which(parts[0]):
            return parts
        if parts[0] == "vcp-worker" and importlib.util.find_spec("vcp_data_plane") is not None:
            return [sys.executable, "-m", "vcp_data_plane.cli", *parts[1:]]
        raise LauncherError(
            f"worker command {parts[0]!r} not found on PATH — install vcp_data_plane on the "
            f"control-plane host or set VCP_CP_WORKER_COMMAND"
        )

    async def check(self) -> tuple[bool, str]:
        try:
            return True, " ".join(self._command())
        except LauncherError as exc:
            return False, str(exc)

    async def start(self, fleet: Any, config_path: Path) -> LaunchResult:
        cmd = [*self._command(), "run", str(config_path)]
        log_path = fleet_dir(self.settings, fleet.fleet_id) / "worker.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log = log_path.open("ab")
        log.write(f"\n===== starting worker {fleet.worker_id}: {' '.join(cmd)}\n".encode())
        log.flush()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=log,
                stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.DEVNULL,
                start_new_session=True,           # survive control-plane restarts / Ctrl-C
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
        finally:
            log.close()
        self._children[proc.pid] = proc
        return LaunchResult(
            runtime={"pid": proc.pid, "command": cmd, "log": str(log_path)},
            worker_url=f"http://{self.settings.worker_host}:{fleet.api_port}",
        )

    def _pid(self, fleet: Any) -> int | None:
        pid = (fleet.runtime or {}).get("pid")
        return int(pid) if pid else None

    async def is_alive(self, fleet: Any) -> bool:
        pid = self._pid(fleet)
        if not pid:
            return False
        child = self._children.get(pid)
        if child is not None:
            return child.returncode is None
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        # Treat zombies as dead (Linux only)
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
            return stat.split(") ", 1)[1][0] != "Z"
        except Exception:  # noqa: BLE001
            return True

    def exit_code(self, fleet: Any) -> int | None:
        pid = self._pid(fleet)
        child = self._children.get(pid) if pid else None
        return child.returncode if child is not None else None

    async def stop(self, fleet: Any, timeout_s: float = 10.0) -> None:
        pid = self._pid(fleet)
        if not pid or not await self.is_alive(fleet):
            return
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                return
        child = self._children.get(pid)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while loop.time() < deadline:
            if child is not None:
                try:
                    await asyncio.wait_for(child.wait(), timeout=0.2)
                except asyncio.TimeoutError:
                    pass
            else:
                await asyncio.sleep(0.2)
            if not await self.is_alive(fleet):
                break
        else:
            logger.warning("worker %s (pid %s) did not stop in %.0fs — killing", fleet.worker_id, pid, timeout_s)
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except Exception:  # noqa: BLE001
                pass
        self._children.pop(pid, None)


# ---------------------------------------------------------------------------
# Docker
# ---------------------------------------------------------------------------

class DockerLauncher(WorkerLauncher):
    name = "docker"

    def uses_container_paths(self) -> bool:
        return True

    def _client(self) -> httpx.AsyncClient:
        transport = httpx.AsyncHTTPTransport(uds=self.settings.docker_socket)
        return httpx.AsyncClient(transport=transport, base_url="http://docker", timeout=30.0)

    @staticmethod
    def container_name(fleet: Any) -> str:
        return f"vcp-worker-{fleet.fleet_id}"

    async def check(self) -> tuple[bool, str]:
        if not Path(self.settings.docker_socket).exists():
            return False, f"Docker socket {self.settings.docker_socket} not found"
        try:
            async with self._client() as c:
                r = await c.get("/version")
                r.raise_for_status()
                return True, f"Docker {r.json().get('Version')} · image {self.settings.docker_image}"
        except Exception as exc:  # noqa: BLE001
            return False, f"Docker API unavailable: {exc}"

    async def start(self, fleet: Any, config_path: Path) -> LaunchResult:
        name = self.container_name(fleet)
        host_dir = (
            str(Path(self.settings.docker_host_fleet_dir) / fleet.fleet_id)
            if self.settings.docker_host_fleet_dir
            else str(config_path.parent)
        )
        port = int(fleet.api_port)
        mport = int(fleet.metrics_port)
        host_config: dict[str, Any] = {
            "Binds": [f"{host_dir}:/config:ro"],
            "RestartPolicy": {"Name": "no"},
            # lets bridged workers reach Redis / a CSMS on the host as host.docker.internal
            "ExtraHosts": ["host.docker.internal:host-gateway"],
        }
        network = self.settings.docker_network
        if network == "host":
            host_config["NetworkMode"] = "host"
            host_config.pop("ExtraHosts")
            url = f"http://{self.settings.worker_host}:{port}"
        elif network:
            # shared user-defined network (e.g. the docker-compose network)
            host_config["NetworkMode"] = network
            url = f"http://{name}:{port}"
        else:
            host_config["PortBindings"] = {
                f"{port}/tcp": [{"HostPort": str(port)}],
                f"{mport}/tcp": [{"HostPort": str(mport)}],
            }
            url = f"http://{self.settings.worker_host}:{port}"
        body = {
            "Image": self.settings.docker_image,
            "Cmd": ["run", "/config/worker.yaml"],
            "Labels": {"vcp.fleet_id": fleet.fleet_id, "vcp.worker_id": fleet.worker_id},
            "ExposedPorts": {f"{port}/tcp": {}, f"{mport}/tcp": {}},
            "Env": ["PYTHONUNBUFFERED=1"],
            "HostConfig": host_config,
        }
        async with self._client() as c:
            await c.delete(f"/containers/{name}", params={"force": "true"})   # stale container, if any
            r = await c.post("/containers/create", params={"name": name}, json=body)
            if r.status_code == 404:
                raise LauncherError(f"Docker image {self.settings.docker_image!r} not found — build it from "
                                    f"vcp_data_plane/docker/Dockerfile")
            if r.status_code >= 400:
                raise LauncherError(f"container create failed: {r.status_code} {r.text}")
            cid = r.json()["Id"]
            r = await c.post(f"/containers/{cid}/start")
            if r.status_code >= 400:
                raise LauncherError(f"container start failed: {r.status_code} {r.text}")
        return LaunchResult(runtime={"container_id": cid, "container_name": name}, worker_url=url)

    async def _inspect(self, fleet: Any) -> dict[str, Any] | None:
        cid = (fleet.runtime or {}).get("container_id") or self.container_name(fleet)
        try:
            async with self._client() as c:
                r = await c.get(f"/containers/{cid}/json")
                return r.json() if r.status_code == 200 else None
        except Exception:  # noqa: BLE001
            return None

    async def is_alive(self, fleet: Any) -> bool:
        info = await self._inspect(fleet)
        return bool(info and info.get("State", {}).get("Running"))

    async def logs(self, fleet: Any, tail: int = 200) -> str:
        cid = (fleet.runtime or {}).get("container_id") or self.container_name(fleet)
        try:
            async with self._client() as c:
                r = await c.get(f"/containers/{cid}/logs",
                                params={"stdout": 1, "stderr": 1, "tail": tail})
                if r.status_code == 200:
                    return _demux_docker_logs(r.content)
        except Exception:  # noqa: BLE001
            pass
        return await super().logs(fleet, tail)

    async def stop(self, fleet: Any, timeout_s: float = 10.0) -> None:
        cid = (fleet.runtime or {}).get("container_id") or self.container_name(fleet)
        async with self._client() as c:
            # keep a log snapshot next to the config before removing the container
            try:
                r = await c.get(f"/containers/{cid}/logs", params={"stdout": 1, "stderr": 1, "tail": 2000})
                if r.status_code == 200:
                    p = fleet_dir(self.settings, fleet.fleet_id) / "worker.log"
                    with p.open("a") as fh:
                        fh.write(_demux_docker_logs(r.content) + "\n")
            except Exception:  # noqa: BLE001
                pass
            await c.post(f"/containers/{cid}/stop", params={"t": int(timeout_s)})
            await c.delete(f"/containers/{cid}", params={"force": "true"})


def _demux_docker_logs(raw: bytes) -> str:
    """Decode Docker's multiplexed log stream (8-byte frame headers)."""
    out, i = [], 0
    while i + 8 <= len(raw) and raw[i] in (0, 1, 2) and raw[i + 1:i + 4] == b"\x00\x00\x00":
        size = int.from_bytes(raw[i + 4:i + 8], "big")
        out.append(raw[i + 8:i + 8 + size])
        i += 8 + size
    if i == 0:   # TTY containers are not multiplexed
        return raw.decode("utf-8", "replace")
    return b"".join(out).decode("utf-8", "replace")


def get_launcher(name: str, settings: Settings) -> WorkerLauncher:
    if name == "process":
        return ProcessLauncher(settings)
    if name == "docker":
        return DockerLauncher(settings)
    raise LauncherError(f"unknown launcher {name!r} (expected one of {LAUNCHERS})")
