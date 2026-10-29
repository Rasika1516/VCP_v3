"""CLI entry point: vcp-worker."""
from __future__ import annotations

import asyncio
import logging
import os
import sys

import click
import uvicorn

from vcp_data_plane.config.loader import ConfigError, load_worker_config
from vcp_data_plane.worker.runner import WorkerRunner


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


@click.group()
@click.option("--log-level", default="INFO", show_default=True, help="Logging level")
def main(log_level: str) -> None:
    """VCP Simulator — Simulation Data Plane worker."""
    _setup_logging(log_level)


# ---------------------------------------------------------------------------
# Run a worker
# ---------------------------------------------------------------------------

@main.command("run")
@click.argument("config_path", type=click.Path(exists=True))
@click.option("--api-port", type=int, default=None, help="Override control_api.port")
@click.option("--metrics-port", type=int, default=None, help="Override metrics.port")
def run_cmd(config_path: str, api_port: int | None, metrics_port: int | None) -> None:
    """Start the VCP data plane worker (all VCPs + command listener + API)."""
    try:
        config = load_worker_config(config_path)
    except ConfigError as exc:
        click.echo(f"Config error: {exc}", err=True)
        for e in exc.errors:
            click.echo(f"  - {e}", err=True)
        sys.exit(1)

    if api_port is not None:
        config.control_api.port = api_port
    if metrics_port is not None:
        config.metrics.port = metrics_port

    asyncio.run(_run_worker(config))


async def _run_worker(config) -> None:
    from vcp_data_plane.api.worker_api import build_worker_api
    from vcp_data_plane.metrics.collector import WorkerMetrics

    runner = WorkerRunner(config)
    await runner.start()

    # Build Worker API
    api_app = build_worker_api(
        worker_id=config.worker_id,
        registry=runner.registry,
        command_dispatcher=runner.dispatch_command,
        control_token=config.control_api.token,
        require_auth=config.control_api.require_auth,
        group_of=lambda vid: (runner.runtimes[vid].entry.group_id if vid in runner.runtimes else None),
    )

    # Build Prometheus metrics endpoint
    from prometheus_client import make_asgi_app
    metrics_app = make_asgi_app()

    api_server = uvicorn.Server(
        uvicorn.Config(
            api_app,
            host=config.control_api.host,
            port=config.control_api.port,
            log_level="warning",
        )
    )
    metrics_server = uvicorn.Server(
        uvicorn.Config(
            metrics_app,
            host=config.metrics.host,
            port=config.metrics.port,
            log_level="warning",
        )
    )

    stop_event = asyncio.Event()

    def _on_signal() -> None:
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(__import__("signal"), sig_name, None)
        if sig is not None:
            try:
                loop.add_signal_handler(sig, _on_signal)
            except (NotImplementedError, OSError):
                pass

    tasks = [
        asyncio.create_task(api_server.serve(), name="worker-api"),
        asyncio.create_task(metrics_server.serve(), name="worker-metrics"),
        asyncio.create_task(stop_event.wait(), name="stop-signal"),
    ]

    click.echo(
        f"Worker {config.worker_id} running: "
        f"API http://{config.control_api.host}:{config.control_api.port}  "
        f"Metrics http://{config.metrics.host}:{config.metrics.port}"
    )

    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

    api_server.should_exit = True
    metrics_server.should_exit = True
    await runner.stop()

    # Let the uvicorn servers finish their own shutdown (should_exit) instead
    # of cancelling them mid-lifespan; cancel only what is still running.
    _, still = await asyncio.wait(pending, timeout=5)
    for t in still:
        t.cancel()
    await asyncio.gather(*still, return_exceptions=True)
    click.echo(f"Worker {config.worker_id} stopped.")


# ---------------------------------------------------------------------------
# Validate config
# ---------------------------------------------------------------------------

@main.command("validate")
@click.argument("config_path", type=click.Path(exists=True))
def validate_cmd(config_path: str) -> None:
    """Validate a worker config YAML without starting the worker."""
    try:
        config = load_worker_config(config_path)
        click.echo(f"✓ Config valid: {config.worker_id} ({len(config.vcps)} VCPs)")
        sys.exit(0)
    except ConfigError as exc:
        click.echo(f"✗ Config error: {exc}", err=True)
        for e in exc.errors:
            click.echo(f"  - {e}", err=True)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Profile management
# ---------------------------------------------------------------------------

@main.command("list-profiles")
@click.argument("profile_dir", type=click.Path(exists=True))
def list_profiles_cmd(profile_dir: str) -> None:
    """List all charger profiles in a directory."""
    from vcp_data_plane.config.loader import load_profiles_dir
    try:
        profiles = load_profiles_dir(profile_dir)
        if not profiles:
            click.echo("No profiles found.")
            return
        click.echo(f"Found {len(profiles)} profile(s):")
        for pid, p in profiles.items():
            click.echo(f"  {pid} v{p.version}  {p.vendor}/{p.model}  OCPP {p.ocpp_version.value}")
    except ConfigError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
