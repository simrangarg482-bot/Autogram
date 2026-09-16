"""add question_ledger to autonomous_tasks

Adds the per-question ledger column backing
`automation/agents/autonomous/question_ledger.py` — the autonomous agent's
counterpart to the deterministic engine's `application_questions` table, kept
as JSONB on the task itself for exactly the same reason `field_attempt_ledger`
and `confirmed_answers` are: it is per-task agent state, not a separately
addressable resource with its own review UI.

NOT NULL with a `'{}'::jsonb` server default, so existing rows are backfilled
in place by the ALTER and no application code ever has to handle a NULL here
(matching `field_attempt_ledger`'s `nullable=False, default=dict` on the
model).

Revision ID: a1c7f0e29b41
Revises: a6b7c8d9e0f1
Create Date: 2026-09-16 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'a1c7f0e29b41'
down_revision: Union[str, Sequence[str], None] = 'a6b7c8d9e0f1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'autonomous_tasks',
        sa.Column(
            'question_ledger',
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('autonomous_tasks', 'question_ledger')
