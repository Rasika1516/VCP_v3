"""VCP fleets — deployable worker definitions + vcp_instance.fleet_id.

Revision ID: 002
Revises: 001
Create Date: 2026-10-28 00:00:00
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def _json():
    return postgresql.JSONB().with_variant(sa.JSON(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "vcp_fleet",
        sa.Column("fleet_id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False, server_default=""),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("worker_id", sa.String(64), nullable=False, unique=True),
        sa.Column("csms_url", sa.String(512), nullable=False),
        sa.Column("csms_auth", _json(), nullable=False, server_default="{}"),
        sa.Column("profile_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("ocpp_version", sa.String(16), nullable=True),
        sa.Column("group_id", sa.String(64), nullable=True),
        sa.Column("id_prefix", sa.String(32), nullable=False, server_default="VCP-"),
        sa.Column("charge_point_id_prefix", sa.String(32), nullable=False, server_default="CP-"),
        sa.Column("id_start", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("options", _json(), nullable=False, server_default="{}"),
        sa.Column("launcher", sa.String(16), nullable=False, server_default="process"),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("api_port", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("metrics_port", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("worker_url", sa.String(256), nullable=False, server_default=""),
        sa.Column("config_path", sa.String(512), nullable=False, server_default=""),
        sa.Column("runtime", _json(), nullable=False, server_default="{}"),
        sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
        sa.Column("config_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("deployed_config_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("deployed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_vcp_fleet_worker_id", "vcp_fleet", ["worker_id"], unique=True)
    op.create_index("ix_vcp_fleet_status", "vcp_fleet", ["status"])

    op.add_column("vcp_instance", sa.Column("fleet_id", sa.String(64), nullable=True))
    op.create_index("ix_vcp_instance_fleet_id", "vcp_instance", ["fleet_id"])

    # Worker-reported command outcome (contract v2 command_result events)
    op.add_column("command_audit", sa.Column("result", _json(), nullable=False, server_default="{}"))


def downgrade() -> None:
    op.drop_column("command_audit", "result")
    op.drop_index("ix_vcp_instance_fleet_id", table_name="vcp_instance")
    op.drop_column("vcp_instance", "fleet_id")
    op.drop_index("ix_vcp_fleet_status", table_name="vcp_fleet")
    op.drop_index("ix_vcp_fleet_worker_id", table_name="vcp_fleet")
    op.drop_table("vcp_fleet")
