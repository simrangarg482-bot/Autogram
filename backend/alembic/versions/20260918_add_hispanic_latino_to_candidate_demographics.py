"""add hispanic_latino to candidate_demographics

Greenhouse (and other boards using the EEOC form) ask "Are you Hispanic/Latino?"
as its own yes/no question, separate from Race. Folding it into the existing
free-text `race_ethnicity` column made that question unanswerable from stored
data, so it gets a dedicated nullable column. `NULL` keeps its meaning
everywhere in this table: "never asked" — no default is ever written.

Guarded with an inspector check so it is safe to run against a database where
the column was already added by hand.

Revision ID: b2d8e4f61a37
Revises: a1c7f0e29b41
Create Date: 2026-09-18 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b2d8e4f61a37'
down_revision: Union[str, Sequence[str], None] = 'a1c7f0e29b41'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column() -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(c["name"] == "hispanic_latino" for c in inspector.get_columns("candidate_demographics"))


def upgrade() -> None:
    """Upgrade schema."""
    if not _has_column():
        op.add_column(
            'candidate_demographics',
            sa.Column('hispanic_latino', sa.String(), nullable=True),
        )


def downgrade() -> None:
    """Downgrade schema."""
    if _has_column():
        op.drop_column('candidate_demographics', 'hispanic_latino')
