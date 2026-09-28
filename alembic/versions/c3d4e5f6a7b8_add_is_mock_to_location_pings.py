"""add_is_mock_to_location_pings

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-09-25 15:02:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, Sequence[str], None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'location_pings',
        sa.Column('is_mock', sa.Boolean(), nullable=False, server_default=sa.text('false'))
    )
    op.create_index('ix_location_pings_is_mock', 'location_pings', ['is_mock'])


def downgrade() -> None:
    op.drop_index('ix_location_pings_is_mock', table_name='location_pings')
    op.drop_column('location_pings', 'is_mock')
