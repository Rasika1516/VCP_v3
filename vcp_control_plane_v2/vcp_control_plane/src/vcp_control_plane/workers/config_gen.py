"""Worker configuration generator.

Turns a ``vcp_fleet`` row + its ``vcp_instance`` rows into the data-plane
``worker.yaml`` and the profile YAML files that worker needs:

    {fleet_config_dir}/{fleet_id}/
        worker.yaml          ← generated, matches vcp_data_plane WorkerConfig
        profiles/<id>.yaml   ← exported from the profile registry (or copied
                               from settings.worker_profiles_dir)
        worker.log           ← process launcher output / docker logs snapshot

The generated file is a plain, self-contained worker config: it can be
copied and run by hand with ``vcp-worker run worker.yaml``.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import yaml

from vcp_control_plane.config.settings import Settings

CONTAINER_CONFIG_DIR = "/config"


class FleetConfigError(Exception):
    """The fleet cannot be turned into a valid worker configuration."""


def fleet_dir(settings: Settings, fleet_id: str) -> Path:
    return Path(settings.fleet_config_dir).resolve() / fleet_id


def profile_yaml_from_row(row: Any) -> dict[str, Any]:
    """Rebuild a data-plane ChargerProfile document from a charger_profile row."""
    doc: dict[str, Any] = {
        "profile_id": row.profile_id,
        "version": row.version or "1.0.0",
        "vendor": row.vendor,
        "model": row.model,
        "firmware": row.firmware,
        "ocpp_version": row.ocpp_version or "1.6J",
    }
    if row.evse_definition:
        doc["evses"] = row.evse_definition
    if row.capability_definition:
        doc["capabilities"] = row.capability_definition
    if row.behavior_definition:
        doc["behavior"] = row.behavior_definition
    proto = row.protocol_definition or {}
    for key in ("security_profile", "transport", "serial_number_prefix"):
        if key in proto:
            doc[key] = proto[key]
    if row.tags:
        doc["tags"] = list(row.tags)
    return doc


def _find_profile_file(directory: str, profile_id: str) -> Path | None:
    if not directory:
        return None
    d = Path(directory)
    if not d.is_dir():
        return None
    for f in sorted(d.glob("*.y*ml")):
        try:
            if (yaml.safe_load(f.read_text()) or {}).get("profile_id") == profile_id:
                return f
        except Exception:  # noqa: BLE001
            continue
    return None


def available_profile_ids(settings: Settings, registry_ids: list[str]) -> list[str]:
    ids = set(registry_ids)
    d = Path(settings.worker_profiles_dir) if settings.worker_profiles_dir else None
    if d and d.is_dir():
        for f in d.glob("*.y*ml"):
            try:
                pid = (yaml.safe_load(f.read_text()) or {}).get("profile_id")
                if pid:
                    ids.add(pid)
            except Exception:  # noqa: BLE001
                continue
    return sorted(ids)


def build_worker_config(
    fleet: Any,
    vcps: list[Any],
    settings: Settings,
    for_container: bool = False,
) -> dict[str, Any]:
    if not vcps:
        raise FleetConfigError("fleet has no VCPs")
    if not fleet.csms_url:
        raise FleetConfigError("fleet has no CSMS URL")

    opts = fleet.options or {}
    auth = fleet.csms_auth or {}
    base = CONTAINER_CONFIG_DIR if for_container else str(fleet_dir(settings, fleet.fleet_id))

    vcp_entries = []
    for v in vcps:
        entry: dict[str, Any] = {
            "vcp_id": v.vcp_id,
            "charge_point_id": v.charge_point_id,
            "profile_id": v.profile_id or fleet.profile_id,
        }
        ocpp = v.ocpp_version if v.ocpp_version and v.ocpp_version != "1.6J" else fleet.ocpp_version
        if ocpp:
            entry["ocpp_version"] = ocpp
        group = v.group_id or fleet.group_id
        if group:
            entry["group_id"] = group
        vcp_entries.append(entry)

    jitter = opts.get("boot_jitter_ms") or [0, 2000]
    redis_host = settings.worker_redis_host or settings.redis_host
    redis_port = settings.worker_redis_port or settings.redis_port

    return {
        "worker_id": fleet.worker_id,
        "csms": {
            "url": fleet.csms_url,
            "auth": {
                "type": auth.get("type") or "none",
                **({"username": auth["username"]} if auth.get("username") else {}),
                **({"password": auth["password"]} if auth.get("password") else {}),
            },
        },
        "redis": {
            "host": redis_host,
            "port": int(redis_port),
            "db": settings.redis_db,
            "password": settings.redis_password or None,
            "ssl": settings.redis_ssl,
            "command_stream_prefix": settings.command_stream_prefix,
            "broadcast_stream": settings.broadcast_stream,
            "event_stream_prefix": settings.event_stream_prefix,
            "forward_traces_to_events": opts.get("forward_traces_to_events") is not False,
            "worker_heartbeat_s": float(opts.get("worker_heartbeat_s") or 15),
        },
        "vcps": vcp_entries,
        "profile_dir": f"{base}/profiles",
        "control_api": {
            "host": "0.0.0.0",
            "port": int(fleet.api_port),
            "token": settings.worker_token or "",
            "require_auth": bool(settings.worker_token),
        },
        "metrics": {"host": "0.0.0.0", "port": int(fleet.metrics_port), "enabled": True},
        "boot_jitter_ms": [int(jitter[0]), int(jitter[1])],
        "log_level": str(opts.get("log_level") or "INFO").upper(),
    }


def write_fleet_files(
    fleet: Any,
    vcps: list[Any],
    profile_rows: dict[str, Any],
    settings: Settings,
    for_container: bool = False,
) -> Path:
    """Write worker.yaml + profiles for a fleet.  Returns the worker.yaml path."""
    cfg = build_worker_config(fleet, vcps, settings, for_container=for_container)
    out = fleet_dir(settings, fleet.fleet_id)
    prof_dir = out / "profiles"
    prof_dir.mkdir(parents=True, exist_ok=True)
    for old in prof_dir.glob("*.yaml"):
        old.unlink()

    needed = sorted({e["profile_id"] for e in cfg["vcps"]})
    missing = []
    for pid in needed:
        row = profile_rows.get(pid)
        if row is not None:
            (prof_dir / f"{pid}.yaml").write_text(yaml.safe_dump(profile_yaml_from_row(row), sort_keys=False))
            continue
        src = _find_profile_file(settings.worker_profiles_dir, pid)
        if src is not None:
            shutil.copyfile(src, prof_dir / f"{pid}.yaml")
            continue
        missing.append(pid)
    if missing:
        raise FleetConfigError(
            f"profile(s) not found in the registry or worker_profiles_dir: {', '.join(missing)}"
        )

    header = (
        f"# Generated by VCP Control Plane for fleet '{fleet.fleet_id}' "
        f"(config v{fleet.config_version}). Do not edit — changes are overwritten on deploy.\n"
    )
    path = out / "worker.yaml"
    path.write_text(header + yaml.safe_dump(cfg, sort_keys=False))
    return path
