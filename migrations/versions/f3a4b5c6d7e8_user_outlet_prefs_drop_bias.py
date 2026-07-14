"""User outlet preferences and drop bias scoring columns.

Revision ID: f3a4b5c6d7e8
Revises: a7b8c9d0e1f2
Create Date: 2026-07-14 09:10:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "f3a4b5c6d7e8"
down_revision = "a7b8c9d0e1f2"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "user_outlet_preferences",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("outlet_id", sa.Integer(), sa.ForeignKey("outlets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("preference", sa.String(length=16), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("user_id", "outlet_id", name="uq_user_outlet_pref"),
    )
    op.create_index("ix_user_outlet_prefs_user", "user_outlet_preferences", ["user_id"])
    op.create_index("ix_user_outlet_prefs_outlet", "user_outlet_preferences", ["outlet_id"])

    with op.batch_alter_table("outlets", schema=None) as batch_op:
        batch_op.add_column(sa.Column("first_seen_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("discovery_source", sa.String(length=32), nullable=True))
        batch_op.drop_column("bias_score")
        batch_op.drop_column("bias_retry_count")
        batch_op.drop_column("static_bias_score")
        batch_op.drop_column("bias_source")

    with op.batch_alter_table("articles", schema=None) as batch_op:
        batch_op.drop_index("ix_articles_bias_score")
        batch_op.drop_column("bias_score")

    with op.batch_alter_table("topics", schema=None) as batch_op:
        batch_op.drop_column("bias_mode")

    # Backfill first_seen_at from earliest article when possible.
    op.execute(
        """
        UPDATE outlets o
        SET first_seen_at = sub.min_fetched
        FROM (
            SELECT outlet_id, MIN(fetched_at) AS min_fetched
            FROM articles
            WHERE outlet_id IS NOT NULL
            GROUP BY outlet_id
        ) AS sub
        WHERE o.id = sub.outlet_id AND o.first_seen_at IS NULL
        """
    )
    op.execute(
        "UPDATE outlets SET first_seen_at = NOW() AT TIME ZONE 'utc' WHERE first_seen_at IS NULL"
    )


def downgrade():
    with op.batch_alter_table("topics", schema=None) as batch_op:
        batch_op.add_column(sa.Column("bias_mode", sa.String(length=16), nullable=True))

    with op.batch_alter_table("articles", schema=None) as batch_op:
        batch_op.add_column(sa.Column("bias_score", sa.Float(), nullable=True))
        batch_op.create_index("ix_articles_bias_score", ["bias_score"])

    with op.batch_alter_table("outlets", schema=None) as batch_op:
        batch_op.add_column(sa.Column("bias_score", sa.Float(), nullable=True))
        batch_op.add_column(
            sa.Column("bias_retry_count", sa.Integer(), server_default="0", nullable=False)
        )
        batch_op.add_column(sa.Column("static_bias_score", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("bias_source", sa.String(length=16), nullable=True))
        batch_op.drop_column("discovery_source")
        batch_op.drop_column("first_seen_at")

    op.drop_index("ix_user_outlet_prefs_outlet", table_name="user_outlet_preferences")
    op.drop_index("ix_user_outlet_prefs_user", table_name="user_outlet_preferences")
    op.drop_table("user_outlet_preferences")
