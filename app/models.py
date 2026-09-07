from datetime import datetime

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    pass


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    username: Mapped[str | None] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text, nullable=False, default="other")  # job_board|community|dm|other
    is_channel: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    source_is_group: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    muted_until: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    priority: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    added_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    backfill_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class RawMessage(Base):
    __tablename__ = "raw_messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("sources.id"), nullable=False)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    thread_id: Mapped[int | None] = mapped_column(BigInteger)
    sender_id: Mapped[int | None] = mapped_column(BigInteger)
    reply_to_id: Mapped[int | None] = mapped_column(BigInteger)
    posted_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    text_norm: Mapped[str] = mapped_column(Text, nullable=False)
    text_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    simhash: Mapped[int] = mapped_column(BigInteger, nullable=False)
    link: Mapped[str | None] = mapped_column(Text)
    is_forward: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    struct_features: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    reject_rule: Mapped[str | None] = mapped_column(Text)  # NULL = passed hard filters
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())


class Match(Base):
    __tablename__ = "matches"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    raw_message_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("raw_messages.id"), nullable=False)
    score: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    signals: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)  # {rule: balls}
    stack_matched: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    compensation: Mapped[str | None] = mapped_column(Text)
    contact: Mapped[str | None] = mapped_column(Text)
    headline: Mapped[str | None] = mapped_column(Text)
    decided_by: Mapped[str] = mapped_column(Text, nullable=False, default="rules")  # rules|ml
    ml_prob: Mapped[float | None] = mapped_column()
    rules_version: Mapped[str] = mapped_column(Text, nullable=False)
    model_version: Mapped[str | None] = mapped_column(Text)
    duplicate_count: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    duplicate_of: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("matches.id"))
    notified_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    notification_msg_id: Mapped[int | None] = mapped_column(BigInteger)
    is_favorite: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="new")  # new|notified|suppressed|digest_pending
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

    raw_message: Mapped["RawMessage"] = relationship(lazy="joined")


class Feedback(Base):
    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    match_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("matches.id"), nullable=False)
    verdict: Mapped[str] = mapped_column(Text, nullable=False)  # good|bad
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())


class Keyword(Base):
    __tablename__ = "keywords"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    dict_type: Mapped[str] = mapped_column(Text, nullable=False)
    # stack_core|stack_periph|hiring_strong|hiring_weak|money|anti|closed|noise|alias
    term: Mapped[str] = mapped_column(Text, nullable=False)
    weight: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    maps_to: Mapped[str | None] = mapped_column(Text)  # для dict_type='alias'
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class RuleWeight(Base):
    __tablename__ = "rule_weights"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    weight: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())


class Author(Base):
    __tablename__ = "authors"

    sender_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    good_count: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    bad_count: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    reputation: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
