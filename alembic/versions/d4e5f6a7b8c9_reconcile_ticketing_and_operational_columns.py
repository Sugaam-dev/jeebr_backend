"""reconcile_ticketing_and_operational_columns

Revision ID: d4e5f6a7b8c9
Revises: bfe079a19047
Create Date: 2026-10-15 13:15:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4e5f6a7b8c9'
down_revision: Union[str, Sequence[str], None] = 'bfe079a19047'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    # 1. Operational columns on 'tickets' table
    if 'tickets' in existing_tables:
        ticket_cols = {c['name'] for c in inspector.get_columns('tickets')}
        with op.batch_alter_table('tickets') as batch_op:
            if 'source' not in ticket_cols:
                batch_op.add_column(sa.Column('source', sa.String(length=50), nullable=False, server_default='CUSTOMER'))
            if 'region' not in ticket_cols:
                batch_op.add_column(sa.Column('region', sa.String(length=100), nullable=True))
            if 'assigned_resource_id' not in ticket_cols:
                batch_op.add_column(sa.Column('assigned_resource_id', sa.Integer(), sa.ForeignKey('resources.id', name='fk_tickets_assigned_resource_id'), nullable=True))
            if 'assigned_at' not in ticket_cols:
                batch_op.add_column(sa.Column('assigned_at', sa.DateTime(), nullable=True))
            if 'approval_status' not in ticket_cols:
                batch_op.add_column(sa.Column('approval_status', sa.String(length=50), nullable=False, server_default='NOT_REQUIRED'))
            if 'approved_by_id' not in ticket_cols:
                batch_op.add_column(sa.Column('approved_by_id', sa.Integer(), sa.ForeignKey('users.id', name='fk_tickets_approved_by_id'), nullable=True))
            if 'approved_at' not in ticket_cols:
                batch_op.add_column(sa.Column('approved_at', sa.DateTime(), nullable=True))
            if 'approval_notes' not in ticket_cols:
                batch_op.add_column(sa.Column('approval_notes', sa.Text(), nullable=True))
            try:
                batch_op.alter_column('customer_id', existing_type=sa.Integer(), nullable=True)
            except Exception:
                pass

        existing_indexes = {i['name'] for i in inspector.get_indexes('tickets')}
        if 'ix_tickets_region' not in existing_indexes:
            try:
                op.create_index('ix_tickets_region', 'tickets', ['region'])
            except Exception:
                pass

    # 2. Operational columns on 'users' table
    if 'users' in existing_tables:
        user_cols = {c['name'] for c in inspector.get_columns('users')}
        with op.batch_alter_table('users') as batch_op:
            if 'phone' not in user_cols:
                batch_op.add_column(sa.Column('phone', sa.String(length=50), nullable=True, server_default='+91 98200 12345'))

    # 3. Operational columns on 'field_otps' table
    if 'field_otps' in existing_tables:
        otp_cols = {c['name'] for c in inspector.get_columns('field_otps')}
        with op.batch_alter_table('field_otps') as batch_op:
            if 'dispatch_code' not in otp_cols:
                batch_op.add_column(sa.Column('dispatch_code', sa.String(length=16), nullable=True))

    # 4. Standard financial defaults on 'customers' table
    if 'customers' in existing_tables:
        with op.batch_alter_table('customers') as batch_op:
            for col, default_val in [
                ('arpu', '295.0'),
                ('actual_arpu', '295.0'),
                ('revenue_30d', '295.0'),
                ('plan_price', '299.0'),
            ]:
                try:
                    batch_op.alter_column(col, server_default=default_val)
                except Exception:
                    pass


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if 'field_otps' in existing_tables:
        otp_cols = {c['name'] for c in inspector.get_columns('field_otps')}
        with op.batch_alter_table('field_otps') as batch_op:
            if 'dispatch_code' in otp_cols:
                batch_op.drop_column('dispatch_code')

    if 'users' in existing_tables:
        user_cols = {c['name'] for c in inspector.get_columns('users')}
        with op.batch_alter_table('users') as batch_op:
            if 'phone' in user_cols:
                batch_op.drop_column('phone')

    if 'tickets' in existing_tables:
        existing_indexes = {i['name'] for i in inspector.get_indexes('tickets')}
        if 'ix_tickets_region' in existing_indexes:
            try:
                op.drop_index('ix_tickets_region', table_name='tickets')
            except Exception:
                pass
        ticket_cols = {c['name'] for c in inspector.get_columns('tickets')}
        with op.batch_alter_table('tickets') as batch_op:
            for col in ('approval_notes', 'approved_at', 'approved_by_id', 'approval_status',
                        'assigned_at', 'assigned_resource_id', 'region', 'source'):
                if col in ticket_cols:
                    batch_op.drop_column(col)
