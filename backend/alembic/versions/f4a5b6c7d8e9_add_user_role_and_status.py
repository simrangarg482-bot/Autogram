"""add users.role and users.status (Phase 0 RBAC)

Revision ID: f4a5b6c7d8e9
Revises: e3f4a5b6c7d8
Create Date: 2026-09-08 00:00:00.000000

Adds account-level RBAC (PLATFORM_ROADMAP.md Phase 0):

- `users.role`   — "member" (default) / "moderator" / "admin"
- `users.status` — "active" (default) / "suspended" / "banned"

Both are NOT NULL with a server_default so existing rows backfill to the
"ordinary active member" state on upgrade. See
`app/models/db_models.py::User` / `VALID_USER_ROLES` / `VALID_USER_STATUSES`.

Note (see README.md): on a fresh Neon database `app/main.py` bootstraps the
current schema via `Base.metadata.create_all` at startup regardless of
Alembic's history, so these columns appear either way. This migration exists
so schema changes stay versioned/reproducible.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f4a5b6c7d8e9'
down_revision: Union[str, Sequence[str], None] = 'e3f4a5b6c7d8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'users',
        sa.Column('role', sa.String(), nullable=False, server_default='member'),
    )
    op.add_column(
        'users',
        sa.Column('status', sa.String(), nullable=False, server_default='active'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('users', 'status')
    op.drop_column('users', 'role')
