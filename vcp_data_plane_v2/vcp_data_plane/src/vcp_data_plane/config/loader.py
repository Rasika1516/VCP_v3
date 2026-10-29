"""Config and profile loaders: YAML → validated Pydantic models."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from vcp_data_plane.config.schema import ChargerProfile, WorkerConfig


class ConfigError(Exception):
    def __init__(self, message: str, errors: list[str] | None = None) -> None:
        super().__init__(message)
        self.errors: list[str] = errors or []


def _load_yaml(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"config file not found: {p}")
    with p.open() as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ConfigError(f"expected YAML mapping at root in {p}")
    return data


def load_worker_config(path: str | Path) -> WorkerConfig:
    """Load and validate worker.yaml → WorkerConfig."""
    data = _load_yaml(path)
    # Expand env-var substitution in string scalars
    data = _expand_env(data)
    try:
        cfg = WorkerConfig.model_validate(data)
    except ValidationError as exc:
        errors = [str(e) for e in exc.errors()]
        raise ConfigError(f"worker config validation failed: {path}", errors) from exc

    # If fleet block provided but vcps list is empty, expand fleet → vcps
    if cfg.fleet is not None and not cfg.vcps:
        cfg = cfg.model_copy(update={"vcps": cfg.fleet.expand()})

    return cfg


def load_profile(path: str | Path) -> ChargerProfile:
    """Load and validate a single charger profile YAML."""
    data = _load_yaml(path)
    try:
        return ChargerProfile.model_validate(data)
    except ValidationError as exc:
        errors = [str(e) for e in exc.errors()]
        raise ConfigError(f"profile validation failed: {path}", errors) from exc


def load_profiles_dir(directory: str | Path) -> dict[str, ChargerProfile]:
    """Load all *.yaml files in directory as ChargerProfile objects.

    Returns a dict keyed by profile_id.  If multiple files declare the same
    profile_id the last one loaded wins (files processed in sorted order).
    """
    d = Path(directory)
    if not d.is_dir():
        raise ConfigError(f"profile directory not found: {d}")

    profiles: dict[str, ChargerProfile] = {}
    for yaml_file in sorted(d.glob("*.yaml")):
        try:
            profile = load_profile(yaml_file)
            profiles[profile.profile_id] = profile
        except ConfigError as exc:
            # Log and skip invalid profile files rather than crashing the worker
            import logging
            logging.getLogger(__name__).warning("Skipping invalid profile %s: %s", yaml_file, exc)

    return profiles


def _expand_env(obj: Any) -> Any:
    """Recursively expand ${VAR} and $VAR in string values."""
    if isinstance(obj, str):
        return os.path.expandvars(obj)
    if isinstance(obj, dict):
        return {k: _expand_env(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_env(item) for item in obj]
    return obj
