"""WebSocket transport configuration (TLS / WSS support)."""
from __future__ import annotations

import ssl
from pathlib import Path

from vcp_data_plane.config.schema import AuthType, CsmsAuthConfig, SecurityProfile


def build_ssl_context(auth: CsmsAuthConfig, security_profile: SecurityProfile) -> ssl.SSLContext | None:
    """Build an SSL context from config.

    Returns None for plain WS: OCPP security profile 0 (unsecured) and
    profile 1 (HTTP Basic over ws://).  Profiles 2/3 use TLS.
    """
    if security_profile in (SecurityProfile.UNSECURED, SecurityProfile.HTTP_BASIC):
        return None

    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)

    # For testing against internal CSMS without a trusted CA
    if auth.ca_cert:
        ctx.load_verify_locations(auth.ca_cert)
    else:
        # Permissive: skip hostname verification for lab / test environments
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    if security_profile == SecurityProfile.TLS_CERT and auth.type == AuthType.TLS_CERT:
        if not auth.client_cert or not auth.client_key:
            raise ValueError("TLS_CERT security profile requires client_cert and client_key")
        ctx.load_cert_chain(certfile=auth.client_cert, keyfile=auth.client_key)

    return ctx


def websocket_url(base_url: str, charge_point_id: str) -> str:
    """Append the charge-point identity to the CSMS WebSocket URL.

    OCPP spec: the CP identity is appended as the final path segment.
    """
    url = base_url.rstrip("/")
    return f"{url}/{charge_point_id}"


def websocket_subprotocol(ocpp_version: str) -> list[str]:
    """Return the OCPP WebSocket sub-protocol header value."""
    if ocpp_version in ("1.6", "1.6J"):
        return ["ocpp1.6"]
    if ocpp_version in ("2.0.1",):
        return ["ocpp2.0.1"]
    return []
