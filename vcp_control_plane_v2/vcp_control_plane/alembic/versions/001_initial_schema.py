"""Initial schema — VCP control plane tables.

Revision ID: 001
Revises: (none)
Create Date: 2026-01-01 00:00:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # vcp_instance
    op.create_table(
        "vcp_instance",
        sa.Column("vcp_id", sa.String(64), primary_key=True),
        sa.Column("charge_point_id", sa.String(128), nullable=False, unique=True),
        sa.Column("profile_id", sa.String(64), nullable=False),
        sa.Column("profile_version", sa.String(32), nullable=False, server_default="latest"),
        sa.Column("firmware", sa.String(64), nullable=True),
        sa.Column("ocpp_version", sa.String(16), nullable=False, server_default="1.6J"),
        sa.Column("transport", sa.String(16), nullable=False, server_default="websocket"),
        sa.Column("security_profile", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("worker_id", sa.String(64), nullable=True),
        sa.Column("group_id", sa.String(64), nullable=True),
        sa.Column("desired_state", sa.String(32), nullable=False, server_default="Available"),
        sa.Column("actual_state", sa.String(32), nullable=False, server_default="Offline"),
        sa.Column("connection_state", sa.String(32), nullable=False, server_default="Disconnected"),
        sa.Column("connector_state", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("tags", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("meta", postgresql.JSONB(), nullable=False, server_default="{}"),
    )
    op.create_index("ix_vcp_instance_worker_id", "vcp_instance", ["worker_id"])
    op.create_index("ix_vcp_instance_group_id", "vcp_instance", ["group_id"])
    op.create_index("ix_vcp_instance_profile_id", "vcp_instance", ["profile_id"])

    # charger_profile
    op.create_table(
        "charger_profile",
        sa.Column("profile_id", sa.String(64), primary_key=True),
        sa.Column("version", sa.String(32), nullable=False, server_default="1.0.0"),
        sa.Column("vendor", sa.String(128), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("firmware", sa.String(64), nullable=False, server_default="1.0.0"),
        sa.Column("ocpp_version", sa.String(16), nullable=False, server_default="1.6J"),
        sa.Column("protocol_definition", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("capability_definition", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("behavior_definition", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("evse_definition", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("source", sa.Text(), nullable=False, server_default=""),
        sa.Column("validated", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("approved_by", sa.String(128), nullable=False, server_default=""),
        sa.Column("tags", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("created_by", sa.String(128), nullable=False, server_default="system"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    # test_execution
    op.create_table(
        "test_execution",
        sa.Column("execution_id", sa.String(64), primary_key=True),
        sa.Column("scenario_id", sa.String(64), nullable=False),
        sa.Column("scenario_name", sa.String(128), nullable=False, server_default=""),
        sa.Column("target_scope", sa.String(16), nullable=False, server_default="single"),
        sa.Column("target_vcp_id", sa.String(64), nullable=True),
        sa.Column("target_group_id", sa.String(64), nullable=True),
        sa.Column("target_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("start_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result", sa.String(32), nullable=True),
        sa.Column("report", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("initiated_by", sa.String(128), nullable=False, server_default="api"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_test_execution_scenario_id", "test_execution", ["scenario_id"])

    # command_audit
    op.create_table(
        "command_audit",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("command_id", sa.String(64), nullable=False, unique=True),
        sa.Column("command_type", sa.String(64), nullable=False),
        sa.Column("vcp_id", sa.String(64), nullable=True),
        sa.Column("group_id", sa.String(64), nullable=True),
        sa.Column("payload", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("issued_by", sa.String(128), nullable=False, server_default="api"),
        sa.Column("status", sa.String(32), nullable=False, server_default="dispatched"),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_command_audit_vcp_id", "command_audit", ["vcp_id"])


def downgrade() -> None:
    op.drop_table("command_audit")
    op.drop_table("test_execution")
    op.drop_table("charger_profile")
    op.drop_table("vcp_instance")
