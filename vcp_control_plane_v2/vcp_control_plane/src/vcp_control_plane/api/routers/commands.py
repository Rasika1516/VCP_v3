"""Command catalogue — the worker command vocabulary (bus contract v2).

GET /commands   → list of {commandType, label, category, payload, options?}

The UI builds its per-VCP and bulk command controls from this list, so it
always matches what data-plane workers can execute.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from vcp_control_plane.api.auth import make_auth_dependency
from vcp_control_plane.bus.contract import COMMAND_CATALOGUE, CONTRACT_VERSION

router = APIRouter()
_auth = make_auth_dependency()


@router.get("/commands", dependencies=[Depends(_auth)])
async def list_commands() -> dict:
    return {"contractVersion": CONTRACT_VERSION, "items": COMMAND_CATALOGUE}
