"""add_field_tracking_and_otp_tables

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-09-23 18:30:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, Sequence[str], None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Customer service coordinates
    op.add_column('customers', sa.Column('service_latitude', sa.Float(), nullable=True))
    op.add_column('customers', sa.Column('service_longitude', sa.Float(), nullable=True))
    op.add_column('customers', sa.Column('service_address', sa.String(length=255), nullable=True))

    # 2. Resource location & user link
    op.add_column('resources', sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True))
    op.add_column('resources', sa.Column('current_latitude', sa.Float(), nullable=True))
    op.add_column('resources', sa.Column('current_longitude', sa.Float(), nullable=True))
    op.add_column('resources', sa.Column('last_ping_at', sa.DateTime(), nullable=True))
    op.add_column('resources', sa.Column('location_status', sa.String(length=50), server_default='UNAVAILABLE', nullable=True))

    # 3. Field Assignments
    op.create_table(
        'field_assignments',
        sa.Column('id', sa.Integer(), nullable=False, primary_key=True),
        sa.Column('market_id', sa.String(length=50), nullable=False, server_default='mumbai'),
        sa.Column('ticket_id', sa.Integer(), sa.ForeignKey('tickets.id', ondelete='CASCADE'), nullable=False, unique=True),
        sa.Column('engineer_id', sa.Integer(), sa.ForeignKey('resources.id', ondelete='CASCADE'), nullable=False),
        sa.Column('status', sa.String(length=50), nullable=False, server_default='ASSIGNED'),
        sa.Column('assigned_at', sa.DateTime(), nullable=True),
        sa.Column('accepted_at', sa.DateTime(), nullable=True),
        sa.Column('en_route_at', sa.DateTime(), nullable=True),
        sa.Column('arrived_at', sa.DateTime(), nullable=True),
        sa.Column('work_started_at', sa.DateTime(), nullable=True),
        sa.Column('otp_requested_at', sa.DateTime(), nullable=True),
        sa.Column('otp_verified_at', sa.DateTime(), nullable=True),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_field_assignments_id', 'field_assignments', ['id'])
    op.create_index('ix_field_assignments_market_id', 'field_assignments', ['market_id'])
    op.create_index('ix_field_assignments_ticket_id', 'field_assignments', ['ticket_id'])
    op.create_index('ix_field_assignments_engineer_id', 'field_assignments', ['engineer_id'])
    op.create_index('ix_field_assignments_status', 'field_assignments', ['status'])

    # 4. Tracking Sessions
    op.create_table(
        'tracking_sessions',
        sa.Column('id', sa.Integer(), nullable=False, primary_key=True),
        sa.Column('market_id', sa.String(length=50), nullable=False, server_default='mumbai'),
        sa.Column('field_assignment_id', sa.Integer(), sa.ForeignKey('field_assignments.id', ondelete='CASCADE'), nullable=False),
        sa.Column('engineer_id', sa.Integer(), sa.ForeignKey('resources.id', ondelete='CASCADE'), nullable=False),
        sa.Column('status', sa.String(length=50), nullable=False, server_default='ACTIVE'),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('ended_at', sa.DateTime(), nullable=True),
        sa.Column('last_location_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_tracking_sessions_id', 'tracking_sessions', ['id'])
    op.create_index('ix_tracking_sessions_field_assignment_id', 'tracking_sessions', ['field_assignment_id'])
    op.create_index('ix_tracking_sessions_engineer_id', 'tracking_sessions', ['engineer_id'])
    op.create_index('ix_tracking_sessions_status', 'tracking_sessions', ['status'])

    # 5. Location Pings
    op.create_table(
        'location_pings',
        sa.Column('id', sa.Integer(), nullable=False, primary_key=True),
        sa.Column('tracking_session_id', sa.Integer(), sa.ForeignKey('tracking_sessions.id', ondelete='CASCADE'), nullable=False),
        sa.Column('engineer_id', sa.Integer(), sa.ForeignKey('resources.id', ondelete='CASCADE'), nullable=False),
        sa.Column('market_id', sa.String(length=50), nullable=False, server_default='mumbai'),
        sa.Column('latitude', sa.Float(), nullable=False),
        sa.Column('longitude', sa.Float(), nullable=False),
        sa.Column('accuracy', sa.Float(), nullable=True, server_default='10.0'),
        sa.Column('speed', sa.Float(), nullable=True, server_default='0.0'),
        sa.Column('heading', sa.Float(), nullable=True, server_default='0.0'),
        sa.Column('recorded_at', sa.DateTime(), nullable=True),
        sa.Column('received_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_location_pings_id', 'location_pings', ['id'])
    op.create_index('ix_location_pings_tracking_session_id', 'location_pings', ['tracking_session_id'])
    op.create_index('ix_location_pings_engineer_id', 'location_pings', ['engineer_id'])
    op.create_index('ix_location_pings_recorded_at', 'location_pings', ['recorded_at'])

    # 6. Field OTPs
    op.create_table(
        'field_otps',
        sa.Column('id', sa.Integer(), nullable=False, primary_key=True),
        sa.Column('field_assignment_id', sa.Integer(), sa.ForeignKey('field_assignments.id', ondelete='CASCADE'), nullable=False),
        sa.Column('ticket_id', sa.Integer(), sa.ForeignKey('tickets.id', ondelete='CASCADE'), nullable=False),
        sa.Column('customer_id', sa.Integer(), sa.ForeignKey('customers.id', ondelete='CASCADE'), nullable=False),
        sa.Column('otp_hash', sa.String(length=255), nullable=False),
        sa.Column('salt', sa.String(length=64), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=True, server_default='0'),
        sa.Column('is_used', sa.Boolean(), nullable=True, server_default='false'),
        sa.Column('is_locked', sa.Boolean(), nullable=True, server_default='false'),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('verified_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_field_otps_id', 'field_otps', ['id'])
    op.create_index('ix_field_otps_field_assignment_id', 'field_otps', ['field_assignment_id'])
    op.create_index('ix_field_otps_ticket_id', 'field_otps', ['ticket_id'])


def downgrade() -> None:
    op.drop_table('field_otps')
    op.drop_table('location_pings')
    op.drop_table('tracking_sessions')
    op.drop_table('field_assignments')
    op.drop_column('resources', 'location_status')
    op.drop_column('resources', 'last_ping_at')
    op.drop_column('resources', 'current_longitude')
    op.drop_column('resources', 'current_latitude')
    op.drop_column('resources', 'user_id')
    op.drop_column('customers', 'service_address')
    op.drop_column('customers', 'service_longitude')
    op.drop_column('customers', 'service_latitude')
