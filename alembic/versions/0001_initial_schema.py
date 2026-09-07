"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-08-05

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sources",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("username", sa.Text),
        sa.Column("kind", sa.Text, nullable=False, server_default="other"),
        sa.Column("is_channel", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("source_is_group", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("muted_until", sa.TIMESTAMP(timezone=True)),
        sa.Column("priority", sa.SmallInteger, nullable=False, server_default="0"),
        sa.Column("added_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
        sa.Column("last_seen_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("backfill_requested", sa.Boolean, nullable=False, server_default=sa.false()),
    )

    op.create_table(
        "raw_messages",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("source_id", sa.BigInteger, sa.ForeignKey("sources.id"), nullable=False),
        sa.Column("message_id", sa.BigInteger, nullable=False),
        sa.Column("thread_id", sa.BigInteger),
        sa.Column("sender_id", sa.BigInteger),
        sa.Column("reply_to_id", sa.BigInteger),
        sa.Column("posted_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("text_norm", sa.Text, nullable=False),
        sa.Column("text_hash", sa.String(64), nullable=False),
        sa.Column("simhash", sa.BigInteger, nullable=False),
        sa.Column("link", sa.Text),
        sa.Column("is_forward", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("struct_features", JSONB, nullable=False, server_default="{}"),
        sa.Column("reject_rule", sa.Text),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
        sa.UniqueConstraint("source_id", "message_id", name="uq_raw_source_message"),
    )
    op.create_index("ix_raw_posted_at", "raw_messages", [sa.text("posted_at DESC")])
    op.create_index("ix_raw_hash", "raw_messages", ["text_hash"])
    op.create_index(
        "ix_raw_passed", "raw_messages", ["id"], postgresql_where=sa.text("reject_rule IS NULL")
    )

    op.create_table(
        "matches",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("raw_message_id", sa.BigInteger, sa.ForeignKey("raw_messages.id"), nullable=False),
        sa.Column("score", sa.SmallInteger, nullable=False),
        sa.Column("signals", JSONB, nullable=False, server_default="{}"),
        sa.Column("stack_matched", sa.ARRAY(sa.Text), nullable=False, server_default="{}"),
        sa.Column("compensation", sa.Text),
        sa.Column("contact", sa.Text),
        sa.Column("headline", sa.Text),
        sa.Column("decided_by", sa.Text, nullable=False, server_default="rules"),
        sa.Column("ml_prob", sa.Float),
        sa.Column("rules_version", sa.Text, nullable=False),
        sa.Column("model_version", sa.Text),
        sa.Column("duplicate_count", sa.SmallInteger, nullable=False, server_default="0"),
        sa.Column("duplicate_of", sa.BigInteger, sa.ForeignKey("matches.id")),
        sa.Column("notified_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("notification_msg_id", sa.BigInteger),
        sa.Column("is_favorite", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("status", sa.Text, nullable=False, server_default="new"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    )
    op.create_index("ix_matches_score", "matches", [sa.text("score DESC"), sa.text("created_at DESC")])
    op.create_index("ix_matches_status", "matches", ["status"])

    op.create_table(
        "feedback",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("match_id", sa.BigInteger, sa.ForeignKey("matches.id"), nullable=False),
        sa.Column("verdict", sa.Text, nullable=False),
        sa.Column("reason", sa.Text),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    )

    op.create_table(
        "keywords",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("dict_type", sa.Text, nullable=False),
        sa.Column("term", sa.Text, nullable=False),
        sa.Column("weight", sa.SmallInteger, nullable=False, server_default="1"),
        sa.Column("maps_to", sa.Text),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.UniqueConstraint("dict_type", "term", name="uq_keywords_dict_term"),
    )

    op.create_table(
        "rule_weights",
        sa.Column("key", sa.Text, primary_key=True),
        sa.Column("weight", sa.SmallInteger, nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    )

    op.create_table(
        "authors",
        sa.Column("sender_id", sa.BigInteger, primary_key=True),
        sa.Column("good_count", sa.SmallInteger, nullable=False, server_default="0"),
        sa.Column("bad_count", sa.SmallInteger, nullable=False, server_default="0"),
        sa.Column("reputation", sa.SmallInteger, nullable=False, server_default="0"),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    )

    op.create_table(
        "settings",
        sa.Column("key", sa.Text, primary_key=True),
        sa.Column("value", JSONB, nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    )


def downgrade() -> None:
    op.drop_table("settings")
    op.drop_table("authors")
    op.drop_table("rule_weights")
    op.drop_table("keywords")
    op.drop_table("feedback")
    op.drop_table("matches")
    op.drop_table("raw_messages")
    op.drop_table("sources")
