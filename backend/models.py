from datetime import datetime
from typing import Any
from sqlalchemy import (
    Boolean,
    DateTime,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from backend.database import Base

JSONType = JSONB().with_variant(JSON(), "sqlite")


class Record(Base):
    """Replaces sheet 'Data'. Stores latest versioned state of every row."""

    __tablename__ = "records"

    tbl: Mapped[str] = mapped_column(String(64), primary_key=True)
    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    row: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("ix_records_tbl", "tbl"),)


class History(Base):
    """Replaces sheet 'SundayHistory'. Append-only mutation journal."""

    __tablename__ = "history"

    mutation_id: Mapped[str] = mapped_column(String(180), primary_key=True)
    hash: Mapped[str] = mapped_column(String(64), nullable=False)
    time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    tbl: Mapped[str] = mapped_column(String(64), nullable=False)
    id: Mapped[str] = mapped_column(String(120), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    base_version: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)

    __table_args__ = (Index("ix_history_tbl_id", "tbl", "id"),)


class Proposal(Base):
    """Replaces sheet 'SundayProposals'. Moderation review queue."""

    __tablename__ = "proposals"

    mutation_id: Mapped[str] = mapped_column(String(180), primary_key=True)
    hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    tbl: Mapped[str] = mapped_column(String(64), nullable=False)
    id: Mapped[str] = mapped_column(String(120), nullable=False)
    base_version: Mapped[int] = mapped_column(Integer, nullable=False)
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    is_delete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reviewed_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_proposals_status", "status"),
        Index("ix_proposals_tbl_id", "tbl", "id"),
    )


class EmailNotified(Base):
    """Replaces sheet 'SundayNotifications'. Tracks emailed pending proposals."""

    __tablename__ = "email_notified"

    mutation_id: Mapped[str] = mapped_column(String(180), primary_key=True)
    notified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class BotUser(Base):
    """Replaces sheet 'SundayBotUsers'. Telegram bot users and reminder preferences."""

    __tablename__ = "bot_users"

    tg_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    username: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    reminders: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class Author(Base):
    """Replaces sheet 'SundayAuthors'. Links proposals/mutations to verified Telegram users."""

    __tablename__ = "authors"

    mutation_id: Mapped[str] = mapped_column(String(180), primary_key=True)
    tg_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    tbl: Mapped[str] = mapped_column(String(64), nullable=False)
    row_id: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("ix_authors_tbl_row_id", "tbl", "row_id"),)


class Contact(Base):
    """Replaces sheet 'SundayContacts'. Club member profiles and verified phones."""

    __tablename__ = "contacts"

    tg_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    first_name: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    last_name: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    username: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    phone: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    email: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    club: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    role: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Rsvp(Base):
    """Replaces sheet 'SundayRSVP'. Event registrations and QR check-ins."""

    __tablename__ = "rsvp"

    code: Mapped[str] = mapped_column(String(12), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(120), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    date: Mapped[str] = mapped_column(String(16), nullable=False)
    club: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    tg_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="going")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    checked_in_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    checked_by: Mapped[str | None] = mapped_column(String(255), nullable=True)

    __table_args__ = (
        UniqueConstraint("event_id", "tg_id", name="uq_rsvp_event_tg"),
        Index("ix_rsvp_event_id", "event_id"),
        Index("ix_rsvp_tg_id", "tg_id"),
    )


class KeyValue(Base):
    """Replaces Script Properties set at runtime (TG_MODERATORS, TG_MOD_CODE, TG_SECRET, TG_BOT_USERNAME)."""

    __tablename__ = "kv"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False, default="")
