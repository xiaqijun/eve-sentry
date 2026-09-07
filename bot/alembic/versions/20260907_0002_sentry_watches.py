"""Add persisted EVE Sentry online-watch rules."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260907_0002"
down_revision: str | None = "20260714_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sentry_watches",
        sa.Column("watch_id", sa.String(length=36), primary_key=True),
        sa.Column("group_openid", sa.String(length=128), nullable=False),
        sa.Column("creator_openid", sa.String(length=128), nullable=False),
        sa.Column("target_type", sa.String(length=16), nullable=False),
        sa.Column("target_value", sa.String(length=255), nullable=False),
        sa.Column("target_key", sa.String(length=255), nullable=False),
        sa.Column("interval_seconds", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("present", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("missing_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_check_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "group_openid",
            "target_type",
            "target_key",
            name="uq_sentry_watch_group_target",
        ),
    )
    op.create_index(
        "ix_sentry_watches_group_openid",
        "sentry_watches",
        ["group_openid"],
    )
    op.create_index(
        "ix_sentry_watches_next_check_at",
        "sentry_watches",
        ["next_check_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_sentry_watches_next_check_at", table_name="sentry_watches")
    op.drop_index("ix_sentry_watches_group_openid", table_name="sentry_watches")
    op.drop_table("sentry_watches")
