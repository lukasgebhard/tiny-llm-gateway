"""Database tables."""

from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(UTC)


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(primary_key=True)
    alias: Mapped[str] = mapped_column(String(100), unique=True)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    allow_external: Mapped[bool] = mapped_column(default=False)
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UsageLog(Base):
    """One row per chat completion request."""

    __tablename__ = "usage_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    api_key_id: Mapped[int] = mapped_column(ForeignKey("api_keys.id"), index=True)
    requested_model: Mapped[str] = mapped_column(String(200))
    backend: Mapped[str | None] = mapped_column(String(100))
    upstream_model: Mapped[str | None] = mapped_column(String(200))
    stream: Mapped[bool]
    status: Mapped[str] = mapped_column(String(30))
    http_status: Mapped[int]
    attempts: Mapped[int]
    fallback: Mapped[bool]
    prompt_tokens: Mapped[int | None]
    completion_tokens: Mapped[int | None]
    latency_ms: Mapped[int]
    ttfb_ms: Mapped[int | None]
