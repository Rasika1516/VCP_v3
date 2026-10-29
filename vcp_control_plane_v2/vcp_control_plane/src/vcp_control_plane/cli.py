"""Typer CLI for the VCP Control Plane.

Commands
--------
  vcp-control run          — start the FastAPI server via uvicorn
  vcp-control db migrate   — run Alembic migrations (upgrade head)
  vcp-control db downgrade — downgrade one revision
  vcp-control db revision  — create a new Alembic revision (autogenerate)
  vcp-control profiles import-dir — bulk-import YAML profiles from the profile_dir
"""
from __future__ import annotations

import asyncio
import subprocess
import sys

import typer
from rich.console import Console

app = typer.Typer(
    name="vcp-control",
    help="VCP Fleet Simulator — Control & Management Plane CLI",
    no_args_is_help=True,
)
db_app = typer.Typer(help="Database migration commands (Alembic wrapper).")
app.add_typer(db_app, name="db")

console = Console()


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------

@app.command()
def run(
    host: str = typer.Option(None, "--host", "-h", help="Override API_HOST"),
    port: int = typer.Option(None, "--port", "-p", help="Override API_PORT"),
    reload: bool = typer.Option(False, "--reload", help="Hot-reload (dev only)"),
    workers: int = typer.Option(1, "--workers", "-w", help="Uvicorn worker count"),
) -> None:
    """Start the VCP Control Plane API server."""
    import uvicorn
    from vcp_control_plane.config.settings import get_settings

    settings = get_settings()
    effective_host = host or settings.api_host
    effective_port = port or settings.api_port

    console.print(
        f"[bold green]VCP Control Plane[/bold green] starting on "
        f"[bold]{effective_host}:{effective_port}[/bold]"
    )

    uvicorn.run(
        "vcp_control_plane.api.app:create_app",
        factory=True,
        host=effective_host,
        port=effective_port,
        reload=reload,
        workers=1 if reload else workers,
        log_level="info",
    )


# ---------------------------------------------------------------------------
# db subcommands
# ---------------------------------------------------------------------------

def _alembic(*args: str) -> int:
    """Run an alembic command as a subprocess (inherits env)."""
    cmd = ["alembic", *args]
    console.print(f"[dim]$ {' '.join(cmd)}[/dim]")
    result = subprocess.run(cmd)
    return result.returncode


@db_app.command("migrate")
def db_migrate(
    revision: str = typer.Argument("head", help="Target revision (default: head)"),
) -> None:
    """Run Alembic upgrade to HEAD (or specified revision)."""
    rc = _alembic("upgrade", revision)
    if rc != 0:
        raise typer.Exit(rc)
    console.print("[bold green]Migration complete.[/bold green]")


@db_app.command("downgrade")
def db_downgrade(
    revision: str = typer.Argument("-1", help="Target revision (default: -1)"),
) -> None:
    """Run Alembic downgrade."""
    rc = _alembic("downgrade", revision)
    if rc != 0:
        raise typer.Exit(rc)
    console.print("[bold yellow]Downgrade complete.[/bold yellow]")


@db_app.command("revision")
def db_revision(
    message: str = typer.Option(..., "--message", "-m", help="Migration message"),
    autogenerate: bool = typer.Option(True, "--autogenerate/--no-autogenerate"),
) -> None:
    """Create a new Alembic migration revision."""
    args = ["revision", "-m", message]
    if autogenerate:
        args.append("--autogenerate")
    rc = _alembic(*args)
    if rc != 0:
        raise typer.Exit(rc)


@db_app.command("history")
def db_history() -> None:
    """Show Alembic migration history."""
    _alembic("history", "--verbose")


@db_app.command("current")
def db_current() -> None:
    """Show current Alembic revision."""
    _alembic("current")


# ---------------------------------------------------------------------------
# profiles subcommand
# ---------------------------------------------------------------------------

profiles_app = typer.Typer(help="Charger profile management.")
app.add_typer(profiles_app, name="profiles")


@profiles_app.command("import-dir")
def profiles_import_dir(
    profile_dir: str = typer.Option(
        None, "--dir", "-d", help="Directory of YAML profiles (overrides settings.profile_dir)"
    ),
) -> None:
    """Bulk-import all YAML profile files from the configured profile directory."""
    import yaml
    from pathlib import Path

    from vcp_control_plane.api.schemas import ProfileCreateIn
    from vcp_control_plane.config.settings import get_settings
    from vcp_control_plane.db.base import get_db_session
    from vcp_control_plane.registry.store import VcpRegistryStore

    settings = get_settings()
    target_dir = Path(profile_dir or settings.profile_dir)
    if not target_dir.is_dir():
        console.print(f"[red]Directory not found:[/red] {target_dir}")
        raise typer.Exit(1)

    async def _run() -> None:
        imported, skipped = [], []
        async for session in get_db_session():
            store = VcpRegistryStore(session)
            for yaml_file in sorted(target_dir.glob("*.yaml")):
                try:
                    raw = yaml.safe_load(yaml_file.read_text())
                    profile_id = raw.get("profile_id", "")
                    if not profile_id:
                        skipped.append(str(yaml_file.name))
                        continue
                    body = ProfileCreateIn.model_validate(
                        {
                            "profileId": profile_id,
                            "version": raw.get("version", "1.0.0"),
                            "vendor": raw.get("vendor", "Unknown"),
                            "model": raw.get("model", "Unknown"),
                            "firmware": raw.get("firmware", "1.0.0"),
                            "ocppVersion": raw.get("ocpp_version", "1.6J"),
                            "capabilityDefinition": raw.get("capabilities", {}),
                            "behaviorDefinition": raw.get("behavior", {}),
                            "evseDefinition": raw.get("evses", []),
                            "source": str(yaml_file),
                            "tags": raw.get("tags", []),
                        }
                    )
                    await store.upsert_profile(body)
                    imported.append(profile_id)
                    console.print(f"  [green]✓[/green] {yaml_file.name}")
                except Exception as exc:
                    skipped.append(f"{yaml_file.name}: {exc}")
                    console.print(f"  [red]✗[/red] {yaml_file.name}: {exc}")
            break

        console.print(
            f"\n[bold]Done.[/bold] Imported: {len(imported)}  Skipped: {len(skipped)}"
        )

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# version
# ---------------------------------------------------------------------------

@app.command()
def version() -> None:
    """Print the package version and exit."""
    from importlib.metadata import version as pkg_version

    try:
        v = pkg_version("vcp-control-plane")
    except Exception:
        v = "0.1.0-dev"
    console.print(f"vcp-control-plane {v}")


if __name__ == "__main__":
    app()
