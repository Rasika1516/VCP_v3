"""Charger profile management endpoints.

Routes
------
GET    /profiles               — list profiles
POST   /profiles               — register a profile
GET    /profiles/{profile_id}  — profile detail
DELETE /profiles/{profile_id}  — deprecate profile
POST   /profiles/import        — import from YAML profile file
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status

from vcp_control_plane.api.auth import make_auth_dependency
from vcp_control_plane.api.deps import DbSession, SettingsDep
from vcp_control_plane.api.schemas import (
    OkOut,
    ProfileCreateIn,
    ProfileListOut,
    ProfileOut,
)
from vcp_control_plane.registry.store import VcpRegistryStore

router = APIRouter()
_auth = make_auth_dependency()


def _profile_to_out(row) -> ProfileOut:
    return ProfileOut.model_validate(
        {
            "profileId": row.profile_id,
            "version": row.version,
            "vendor": row.vendor,
            "model": row.model,
            "firmware": row.firmware,
            "ocppVersion": row.ocpp_version,
            "capabilityDefinition": row.capability_definition or {},
            "behaviorDefinition": row.behavior_definition or {},
            "evseDefinition": row.evse_definition or [],
            "status": row.status,
            "validated": row.validated,
            "tags": row.tags or [],
            "createdAt": row.created_at,
        }
    )


@router.get("/profiles", response_model=ProfileListOut, dependencies=[Depends(_auth)])
async def list_profiles(db: DbSession) -> ProfileListOut:
    store = VcpRegistryStore(db)
    rows = await store.list_profiles()
    return ProfileListOut(items=[_profile_to_out(r) for r in rows], total=len(rows))


@router.post("/profiles", response_model=ProfileOut, status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(_auth)])
async def create_profile(body: ProfileCreateIn, db: DbSession) -> ProfileOut:
    store = VcpRegistryStore(db)
    row = await store.upsert_profile(body)
    return _profile_to_out(row)


@router.get("/profiles/{profile_id}", response_model=ProfileOut, dependencies=[Depends(_auth)])
async def get_profile(profile_id: str, db: DbSession) -> ProfileOut:
    store = VcpRegistryStore(db)
    row = await store.get_profile(profile_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Profile '{profile_id}' not found")
    return _profile_to_out(row)


@router.delete("/profiles/{profile_id}", response_model=OkOut, dependencies=[Depends(_auth)])
async def deprecate_profile(profile_id: str, db: DbSession) -> OkOut:
    store = VcpRegistryStore(db)
    ok = await store.deprecate_profile(profile_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Profile '{profile_id}' not found")
    return OkOut(message=f"Profile '{profile_id}' deprecated")


@router.post("/profiles/import-yaml", response_model=ProfileOut,
             status_code=status.HTTP_201_CREATED, dependencies=[Depends(_auth)])
async def import_yaml_profile(file: UploadFile = File(...), db: DbSession = None) -> ProfileOut:
    """Import a charger profile from an uploaded YAML file."""
    content = await file.read()
    try:
        raw = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid YAML: {exc}")

    profile_id = raw.get("profile_id")
    if not profile_id:
        raise HTTPException(status_code=422, detail="YAML must contain 'profile_id' field")

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
            "source": file.filename or "",
            "tags": raw.get("tags", []),
        }
    )
    store = VcpRegistryStore(db)
    row = await store.upsert_profile(body)
    return _profile_to_out(row)


@router.post("/profiles/import-dir", response_model=dict, dependencies=[Depends(_auth)])
async def import_profile_dir(db: DbSession, settings: SettingsDep) -> dict:
    """Scan the configured profile_dir and import / refresh all YAML profiles."""
    profile_dir = Path(settings.profile_dir)
    if not profile_dir.is_dir():
        raise HTTPException(status_code=404,
                            detail=f"Profile directory '{settings.profile_dir}' not found")
    store = VcpRegistryStore(db)
    imported, skipped = [], []
    for yaml_file in sorted(profile_dir.glob("*.yaml")):
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
        except Exception as exc:
            skipped.append(f"{yaml_file.name}: {exc}")

    return {"imported": imported, "skipped": skipped, "total": len(imported)}
