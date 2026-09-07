"""seed keyword dictionaries and rule weights

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-05

"""
import os
import sys

from alembic import op
import sqlalchemy as sa

sys.path.insert(0, os.getcwd())
from app.dictionaries import (  # noqa: E402
    ALIASES, ANTI, CLOSED, HIRING_STRONG, HIRING_WEAK, MONEY, NOISE,
    STACK_CORE, STACK_PERIPH,
)

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

keywords_table = sa.table(
    "keywords",
    sa.column("dict_type", sa.Text),
    sa.column("term", sa.Text),
    sa.column("weight", sa.SmallInteger),
    sa.column("maps_to", sa.Text),
    sa.column("enabled", sa.Boolean),
)

rule_weights_table = sa.table(
    "rule_weights",
    sa.column("key", sa.Text),
    sa.column("weight", sa.SmallInteger),
)

RULE_WEIGHTS = {
    "has_sections": 3,
    "has_contact": 2,
    "has_money_pattern": 2,
    "has_bullets": 2,
    "long_len": 1,
    "short_len": -3,
    "reply_penalty": -2,
    "many_hashtags": -2,
    "source_job_board": 3,
    "source_dm": 2,
    "author_reputation_bonus": 2,
    "noise_term_penalty": -4,
}


def _rows(dict_type: str, terms: list[str], weight: int) -> list[dict]:
    return [{"dict_type": dict_type, "term": t, "weight": weight, "maps_to": None, "enabled": True} for t in terms]


def upgrade() -> None:
    rows = []
    rows += _rows("stack_core", STACK_CORE, 3)
    rows += _rows("stack_periph", STACK_PERIPH, 1)
    rows += _rows("hiring_strong", HIRING_STRONG, 4)
    rows += _rows("hiring_weak", HIRING_WEAK, 2)
    rows += _rows("money", MONEY, 2)
    rows += _rows("anti", ANTI, 1)
    rows += _rows("closed", CLOSED, 1)
    rows += _rows("noise", NOISE, -4)
    rows += [
        {"dict_type": "alias", "term": term, "weight": 0, "maps_to": canon, "enabled": True}
        for term, canon in ALIASES.items()
    ]
    op.bulk_insert(keywords_table, rows)

    op.bulk_insert(
        rule_weights_table,
        [{"key": k, "weight": v} for k, v in RULE_WEIGHTS.items()],
    )


def downgrade() -> None:
    op.execute("DELETE FROM keywords")
    op.execute("DELETE FROM rule_weights")
