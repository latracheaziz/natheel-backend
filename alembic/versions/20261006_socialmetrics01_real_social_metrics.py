"""Store timestamped social account and publication metrics.

Revision ID: socialmetrics01
Revises: 0051e98cc385
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "socialmetrics01"
down_revision = "0051e98cc385"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "social_metric_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("platform", sa.String(length=32), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("username", sa.String(length=255), nullable=True),
        sa.Column("followers_count", sa.BigInteger(), nullable=True),
        sa.Column("following_count", sa.BigInteger(), nullable=True),
        sa.Column("posts_count", sa.BigInteger(), nullable=True),
        sa.Column("likes_count", sa.BigInteger(), nullable=True),
        sa.Column("views_count", sa.BigInteger(), nullable=True),
        sa.Column(
            "raw",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_social_metric_snapshots")),
    )
    op.create_index(
        "ix_social_metric_snapshots_platform_captured",
        "social_metric_snapshots",
        ["platform", "captured_at"],
        unique=False,
    )
    op.create_table(
        "publication_metrics",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("hub_post_id", sa.String(length=64), nullable=False),
        sa.Column("platform", sa.String(length=32), nullable=False),
        sa.Column("external_post_id", sa.String(length=255), nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("likes_count", sa.BigInteger(), nullable=True),
        sa.Column("comments_count", sa.BigInteger(), nullable=True),
        sa.Column("shares_count", sa.BigInteger(), nullable=True),
        sa.Column("views_count", sa.BigInteger(), nullable=True),
        sa.Column(
            "raw",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_publication_metrics")),
    )
    op.create_index(
        "ix_publication_metrics_post_captured",
        "publication_metrics",
        ["hub_post_id", "captured_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_publication_metrics_post_captured", table_name="publication_metrics")
    op.drop_table("publication_metrics")
    op.drop_index("ix_social_metric_snapshots_platform_captured", table_name="social_metric_snapshots")
    op.drop_table("social_metric_snapshots")
