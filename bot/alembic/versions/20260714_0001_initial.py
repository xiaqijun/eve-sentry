"""Initial schema.

Revision ID: 20260714_0001
Revises:
Create Date: 2026-07-14
"""

import sqlalchemy as sa

from alembic import op

revision = "20260714_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "characters",
        sa.Column("character_id", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("corporation_id", sa.BigInteger(), nullable=False),
        sa.Column("corporation_name", sa.String(length=255), nullable=False),
        sa.Column("alliance_id", sa.BigInteger(), nullable=True),
        sa.Column("alliance_name", sa.String(length=255), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_characters_name", "characters", ["name"])

    op.create_table(
        "ship_types",
        sa.Column("type_id", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("group_id", sa.BigInteger(), nullable=False),
        sa.Column("group_name", sa.String(length=255), nullable=False),
        sa.Column("category_id", sa.BigInteger(), nullable=True),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "killmails",
        sa.Column("killmail_id", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("killmail_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("solar_system_id", sa.BigInteger(), nullable=False),
        sa.Column("solo", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("total_value", sa.Float(), nullable=True),
    )
    op.create_index("ix_killmails_killmail_time", "killmails", ["killmail_time"])
    op.create_index("ix_killmails_solar_system_id", "killmails", ["solar_system_id"])

    op.create_table(
        "killmail_participants",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "killmail_id",
            sa.BigInteger(),
            sa.ForeignKey("killmails.killmail_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("character_id", sa.BigInteger(), nullable=True),
        sa.Column("corporation_id", sa.BigInteger(), nullable=True),
        sa.Column("alliance_id", sa.BigInteger(), nullable=True),
        sa.Column("ship_type_id", sa.BigInteger(), nullable=True),
        sa.Column("is_victim", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("final_blow", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_participant_killmail", "killmail_participants", ["killmail_id"])
    op.create_index("ix_participant_character", "killmail_participants", ["character_id"])

    op.create_table(
        "fetch_states",
        sa.Column("character_id", sa.BigInteger(), primary_key=True),
        sa.Column("direction", sa.String(length=16), primary_key=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("truncated", sa.Boolean(), nullable=False, server_default=sa.false()),
    )

    op.create_table(
        "analysis_jobs",
        sa.Column("request_id", sa.String(length=36), primary_key=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("requested_count", sa.Integer(), nullable=False),
        sa.Column("resolved_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("data_events", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
    )
    op.create_index("ix_analysis_jobs_status", "analysis_jobs", ["status"])


def downgrade() -> None:
    op.drop_index("ix_analysis_jobs_status", table_name="analysis_jobs")
    op.drop_table("analysis_jobs")
    op.drop_table("fetch_states")
    op.drop_index("ix_participant_character", table_name="killmail_participants")
    op.drop_index("ix_participant_killmail", table_name="killmail_participants")
    op.drop_table("killmail_participants")
    op.drop_index("ix_killmails_solar_system_id", table_name="killmails")
    op.drop_index("ix_killmails_killmail_time", table_name="killmails")
    op.drop_table("killmails")
    op.drop_table("ship_types")
    op.drop_index("ix_characters_name", table_name="characters")
    op.drop_table("characters")
