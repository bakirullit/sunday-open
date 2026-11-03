"""Initial PostgreSQL 16 schema for Sunday Open

Revision ID: 0001_initial_schema
Revises: 
Create Date: 2026-11-03 14:14:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSONType = postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "records",
        sa.Column("tbl", sa.String(length=64), nullable=False),
        sa.Column("id", sa.String(length=120), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("row", JSONType, nullable=True),
        sa.Column("deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("tbl", "id"),
    )
    op.create_index("ix_records_tbl", "records", ["tbl"], unique=False)

    op.create_table(
        "history",
        sa.Column("mutation_id", sa.String(length=180), nullable=False),
        sa.Column("hash", sa.String(length=64), nullable=False),
        sa.Column(
            "time",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("tbl", sa.String(length=64), nullable=False),
        sa.Column("id", sa.String(length=120), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("base_version", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("before", JSONType, nullable=True),
        sa.Column("after", JSONType, nullable=True),
        sa.PrimaryKeyConstraint("mutation_id"),
    )
    op.create_index("ix_history_tbl_id", "history", ["tbl", "id"], unique=False)

    op.create_table(
        "proposals",
        sa.Column("mutation_id", sa.String(length=180), nullable=False),
        sa.Column("hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("tbl", sa.String(length=64), nullable=False),
        sa.Column("id", sa.String(length=120), nullable=False),
        sa.Column("base_version", sa.Integer(), nullable=False),
        sa.Column("before", JSONType, nullable=True),
        sa.Column("after", JSONType, nullable=True),
        sa.Column("is_delete", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_by", sa.String(length=255), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("mutation_id"),
    )
    op.create_index("ix_proposals_status", "proposals", ["status"], unique=False)
    op.create_index("ix_proposals_tbl_id", "proposals", ["tbl", "id"], unique=False)

    op.create_table(
        "email_notified",
        sa.Column("mutation_id", sa.String(length=180), nullable=False),
        sa.Column(
            "notified_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("mutation_id"),
    )

    op.create_table(
        "bot_users",
        sa.Column("tg_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("username", sa.String(length=255), nullable=False, server_default=""),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("reminders", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.PrimaryKeyConstraint("tg_id"),
    )

    op.create_table(
        "authors",
        sa.Column("mutation_id", sa.String(length=180), nullable=False),
        sa.Column("tg_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("tbl", sa.String(length=64), nullable=False),
        sa.Column("row_id", sa.String(length=120), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("mutation_id"),
    )
    op.create_index("ix_authors_tbl_row_id", "authors", ["tbl", "row_id"], unique=False)

    op.create_table(
        "contacts",
        sa.Column("tg_id", sa.String(length=64), nullable=False),
        sa.Column("first_name", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("last_name", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("username", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("phone", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("email", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("club", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("role", sa.String(length=64), nullable=False, server_default=""),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("tg_id"),
    )

    op.create_table(
        "rsvp",
        sa.Column("code", sa.String(length=12), nullable=False),
        sa.Column("event_id", sa.String(length=120), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("date", sa.String(length=16), nullable=False),
        sa.Column("club", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("tg_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="going"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("checked_in_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("checked_by", sa.String(length=255), nullable=True),
        sa.PrimaryKeyConstraint("code"),
        sa.UniqueConstraint("event_id", "tg_id", name="uq_rsvp_event_tg"),
    )
    op.create_index("ix_rsvp_event_id", "rsvp", ["event_id"], unique=False)
    op.create_index("ix_rsvp_tg_id", "rsvp", ["tg_id"], unique=False)

    op.create_table(
        "kv",
        sa.Column("key", sa.String(length=120), nullable=False),
        sa.Column("value", sa.Text(), nullable=False, server_default=""),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    op.drop_table("kv")
    op.drop_index("ix_rsvp_tg_id", table_name="rsvp")
    op.drop_index("ix_rsvp_event_id", table_name="rsvp")
    op.drop_table("rsvp")
    op.drop_table("contacts")
    op.drop_index("ix_authors_tbl_row_id", table_name="authors")
    op.drop_table("authors")
    op.drop_table("bot_users")
    op.drop_table("email_notified")
    op.drop_index("ix_proposals_tbl_id", table_name="proposals")
    op.drop_index("ix_proposals_status", table_name="proposals")
    op.drop_table("proposals")
    op.drop_index("ix_history_tbl_id", table_name="history")
    op.drop_table("history")
    op.drop_index("ix_records_tbl", table_name="records")
    op.drop_table("records")

