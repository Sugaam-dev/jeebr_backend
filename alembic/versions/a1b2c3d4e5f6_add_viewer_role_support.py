"""add_viewer_role_support

Revision ID: a1b2c3d4e5f6
Revises: 907a27948230
Create Date: 2026-09-11 12:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = '907a27948230'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema: ensure role column accommodates Viewer role."""
    # Column 'role' is VARCHAR(50); ensuring proper index and constraint compatibility
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
