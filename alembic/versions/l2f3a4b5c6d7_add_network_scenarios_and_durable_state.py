"""add_network_scenarios_and_durable_state

Revision ID: l2f3a4b5c6d7
Revises: k1e2f3a4b5c6
Create Date: 2026-10-26 16:30:00.000000

SentinelOS Gate 5: Operational Reliability & Durable State Migration:
1. Creates persistent network_scenarios table for authoritative fault scenario tracking across multi-worker deployments.
2. Persists pre-fault snapshots, alarms generated, active status, and idempotency keys.
3. Safe, dialect-aware, and idempotent execution guards for SQLite and PostgreSQL.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'l2f3a4b5c6d7'
down_revision: Union[str, Sequence[str], None] = 'k1e2f3a4b5c6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    context = op.get_context()
    if getattr(context, 'as_sql', False) or 'MockConnection' in type(bind).__name__:
        existing_tables = set()
        rec_cols = set()
    else:
        inspector = sa.inspect(bind)
        existing_tables = set(inspector.get_table_names())
        rec_cols = {c['name'] for c in inspector.get_columns('recommendations')} if 'recommendations' in existing_tables else set()

    if 'network_scenarios' not in existing_tables:
        op.create_table(
            'network_scenarios',
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
            sa.Column('scenario_id', sa.String(length=64), nullable=False),
            sa.Column('scenario_type', sa.String(length=32), nullable=False),
            sa.Column('target_type', sa.String(length=16), nullable=False),
            sa.Column('target_id', sa.String(length=64), nullable=False),
            sa.Column('market_id', sa.String(length=32), server_default='mumbai', nullable=False),
            sa.Column('severity', sa.String(length=16), server_default='CRITICAL', nullable=False),
            sa.Column('status', sa.String(length=32), server_default='ACTIVE', nullable=False),
            sa.Column('parameters', sa.JSON(), nullable=True),
            sa.Column('snapshot', sa.JSON(), nullable=True),
            sa.Column('affected_links_count', sa.Integer(), server_default='0', nullable=False),
            sa.Column('affected_devices_count', sa.Integer(), server_default='0', nullable=False),
            sa.Column('affected_ports_count', sa.Integer(), server_default='0', nullable=False),
            sa.Column('affected_customers_count', sa.Integer(), server_default='0', nullable=False),
            sa.Column('alarms_generated_count', sa.Integer(), server_default='0', nullable=False),
            sa.Column('alarms_generated', sa.JSON(), nullable=True),
            sa.Column('summary', sa.Text(), nullable=True),
            sa.Column('idempotency_key', sa.String(length=128), nullable=True),
            sa.Column('created_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
            sa.Column('recovered_at', sa.DateTime(), nullable=True),
            sa.Column('recovered_by_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        )
        op.create_index('ix_network_scenarios_scenario_id', 'network_scenarios', ['scenario_id'], unique=True)
        op.create_index('ix_network_scenarios_market_id', 'network_scenarios', ['market_id'])
        op.create_index('ix_network_scenarios_status', 'network_scenarios', ['status'])
        op.create_index('ix_network_scenarios_target_id', 'network_scenarios', ['target_id'])
        op.create_index('ix_network_scenarios_idempotency_key', 'network_scenarios', ['idempotency_key'], unique=True)

    # Reconcile AI recommendation governance columns
    if 'recommendations' in existing_tables:
        with op.batch_alter_table('recommendations') as batch_op:
            if 'recommendation_code' not in rec_cols:
                batch_op.add_column(sa.Column('recommendation_code', sa.String(length=50), nullable=True))
                batch_op.create_index('ix_recommendations_recommendation_code', ['recommendation_code'], unique=True)
            if 'source_alarm_id' not in rec_cols:
                batch_op.add_column(sa.Column('source_alarm_id', sa.Integer(), nullable=True))
            if 'source_alarm_code' not in rec_cols:
                batch_op.add_column(sa.Column('source_alarm_code', sa.String(length=100), nullable=True))
            if 'incident_type' not in rec_cols:
                batch_op.add_column(sa.Column('incident_type', sa.String(length=50), nullable=True))
            if 'proposed_action' not in rec_cols:
                batch_op.add_column(sa.Column('proposed_action', sa.String(length=100), nullable=True))
            if 'confidence_basis' not in rec_cols:
                batch_op.add_column(sa.Column('confidence_basis', sa.String(length=255), nullable=True))
            if 'risk_level' not in rec_cols:
                batch_op.add_column(sa.Column('risk_level', sa.String(length=20), server_default='MEDIUM', nullable=False))
            if 'evidence' not in rec_cols:
                batch_op.add_column(sa.Column('evidence', sa.JSON(), nullable=True))
            if 'impact_assessment' not in rec_cols:
                batch_op.add_column(sa.Column('impact_assessment', sa.JSON(), nullable=True))
            if 'ai_provider' not in rec_cols:
                batch_op.add_column(sa.Column('ai_provider', sa.String(length=50), server_default='DETERMINISTIC_RULES_ENGINE_FALLBACK', nullable=True))
            if 'is_ai_generated' not in rec_cols:
                batch_op.add_column(sa.Column('is_ai_generated', sa.Boolean(), server_default='false', nullable=True))
            if 'expires_at' not in rec_cols:
                batch_op.add_column(sa.Column('expires_at', sa.DateTime(), nullable=True))
            if 'rejection_reason' not in rec_cols:
                batch_op.add_column(sa.Column('rejection_reason', sa.Text(), nullable=True))
            if 'action_fingerprint' not in rec_cols:
                batch_op.add_column(sa.Column('action_fingerprint', sa.String(length=64), nullable=True))
            if 'executed_by_id' not in rec_cols:
                batch_op.add_column(sa.Column('executed_by_id', sa.Integer(), nullable=True))
            if 'executed_at' not in rec_cols:
                batch_op.add_column(sa.Column('executed_at', sa.DateTime(), nullable=True))
            if 'execution_status' not in rec_cols:
                batch_op.add_column(sa.Column('execution_status', sa.String(length=50), nullable=True))
            if 'execution_result' not in rec_cols:
                batch_op.add_column(sa.Column('execution_result', sa.JSON(), nullable=True))
            if 'execution_idempotency_key' not in rec_cols:
                batch_op.add_column(sa.Column('execution_idempotency_key', sa.String(length=100), nullable=True))
                batch_op.create_index('ix_recommendations_execution_idempotency_key', ['execution_idempotency_key'], unique=True)
            if 'retry_count' not in rec_cols:
                batch_op.add_column(sa.Column('retry_count', sa.Integer(), server_default='0', nullable=True))
            if 'max_retries' not in rec_cols:
                batch_op.add_column(sa.Column('max_retries', sa.Integer(), server_default='2', nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    context = op.get_context()
    if getattr(context, 'as_sql', False) or 'MockConnection' in type(bind).__name__:
        existing_tables = {'network_scenarios'}
    else:
        inspector = sa.inspect(bind)
        existing_tables = set(inspector.get_table_names())

    if 'network_scenarios' in existing_tables:
        op.drop_table('network_scenarios')
