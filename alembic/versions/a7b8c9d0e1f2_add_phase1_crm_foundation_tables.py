"""add_phase1_crm_foundation_tables

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-10-16 14:00:00.000000

Phase 1 Stage 2: Schema-only migration introducing the CRM customer master,
contacts, addresses, documents, account users, and activities without altering
or mutating legacy operational tables.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7b8c9d0e1f2'
down_revision: Union[str, Sequence[str], None] = 'f6a7b8c9d0e1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    context = op.get_context()
    if getattr(context, 'as_sql', False) or 'MockConnection' in type(bind).__name__:
        existing_tables = set()
    else:
        inspector = sa.inspect(bind)
        existing_tables = set(inspector.get_table_names())

    # 1. Create 'crm_customers' table
    if 'crm_customers' not in existing_tables:
        op.create_table(
            'crm_customers',
            sa.Column('id', sa.CHAR(36), nullable=False, primary_key=True),
            sa.Column('customer_number', sa.String(length=32), nullable=False),
            sa.Column('legal_name', sa.String(length=255), nullable=False),
            sa.Column('trade_name', sa.String(length=255), nullable=True),
            sa.Column('account_type', sa.String(length=32), nullable=False, server_default='RESIDENTIAL'),
            sa.Column('status', sa.String(length=32), nullable=False, server_default='PROSPECT'),
            sa.Column('market_id', sa.String(length=32), nullable=False),
            sa.Column('legacy_customer_id', sa.Integer(), sa.ForeignKey('customers.id', ondelete='SET NULL'), nullable=True),
            sa.Column('tax_id', sa.String(length=64), nullable=True),
            sa.Column('billing_currency', sa.String(length=3), nullable=False, server_default='INR'),
            sa.Column('is_archived', sa.Boolean(), nullable=False, server_default=sa.text('0' if bind.dialect.name == 'sqlite' else 'false')),
            sa.Column('archived_at', sa.DateTime(), nullable=True),
            sa.Column('archived_by_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint(
                "status IN ('PROSPECT', 'PENDING_VERIFICATION', 'ACTIVE', 'DORMANT', 'SUSPENDED', 'TERMINATED')",
                name='ck_crm_customer_status'
            ),
            sa.CheckConstraint(
                "account_type IN ('RESIDENTIAL', 'ENTERPRISE', 'SME')",
                name='ck_crm_customer_type'
            ),
        )
        op.create_index('ix_crm_customers_customer_number', 'crm_customers', ['customer_number'], unique=True)
        op.create_index('ix_crm_customers_legal_name', 'crm_customers', ['legal_name'])
        op.create_index('ix_crm_customers_market_id', 'crm_customers', ['market_id'])
        op.create_index('ix_crm_customers_status', 'crm_customers', ['status'])
        op.create_index('ix_crm_customers_legacy_customer_id', 'crm_customers', ['legacy_customer_id'], unique=True)
        op.create_index('ix_crm_customers_is_archived', 'crm_customers', ['is_archived'])
        op.create_index('ix_crm_customers_market_status', 'crm_customers', ['market_id', 'status'])

    # 2. Create 'crm_contacts' table
    if 'crm_contacts' not in existing_tables:
        op.create_table(
            'crm_contacts',
            sa.Column('id', sa.CHAR(36), nullable=False, primary_key=True),
            sa.Column('crm_customer_id', sa.CHAR(36), sa.ForeignKey('crm_customers.id', ondelete='CASCADE'), nullable=False),
            sa.Column('first_name', sa.String(length=128), nullable=False),
            sa.Column('last_name', sa.String(length=128), nullable=True),
            sa.Column('email', sa.String(length=255), nullable=True),
            sa.Column('phone', sa.String(length=32), nullable=True),
            sa.Column('role', sa.String(length=64), nullable=False, server_default='PRIMARY'),
            sa.Column('is_primary', sa.Boolean(), nullable=False, server_default=sa.text('0' if bind.dialect.name == 'sqlite' else 'false')),
            sa.Column('is_verified', sa.Boolean(), nullable=False, server_default=sa.text('0' if bind.dialect.name == 'sqlite' else 'false')),
            sa.Column('is_archived', sa.Boolean(), nullable=False, server_default=sa.text('0' if bind.dialect.name == 'sqlite' else 'false')),
            sa.Column('archived_at', sa.DateTime(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint(
                "role IN ('PRIMARY', 'BILLING', 'TECHNICAL', 'EMERGENCY')",
                name='ck_crm_contact_role'
            ),
        )
        op.create_index('ix_crm_contacts_crm_customer_id', 'crm_contacts', ['crm_customer_id'])
        op.create_index('ix_crm_contacts_email', 'crm_contacts', ['email'])
        op.create_index('ix_crm_contacts_phone', 'crm_contacts', ['phone'])
        op.create_index('ix_crm_contacts_is_archived', 'crm_contacts', ['is_archived'])

        # Cross-dialect partial unique index for primary contact
        if bind.dialect.name == 'postgresql':
            op.execute("""
                CREATE UNIQUE INDEX uq_crm_contacts_customer_primary
                ON crm_contacts (crm_customer_id)
                WHERE is_primary = true AND is_archived = false;
            """)
        else:
            op.execute("""
                CREATE UNIQUE INDEX uq_crm_contacts_customer_primary
                ON crm_contacts (crm_customer_id)
                WHERE is_primary = 1 AND is_archived = 0;
            """)

    # 3. Create 'crm_addresses' table
    if 'crm_addresses' not in existing_tables:
        op.create_table(
            'crm_addresses',
            sa.Column('id', sa.CHAR(36), nullable=False, primary_key=True),
            sa.Column('crm_customer_id', sa.CHAR(36), sa.ForeignKey('crm_customers.id', ondelete='CASCADE'), nullable=False),
            sa.Column('address_type', sa.String(length=32), nullable=False, server_default='INSTALLATION'),
            sa.Column('address_line1', sa.String(length=255), nullable=False),
            sa.Column('address_line2', sa.String(length=255), nullable=True),
            sa.Column('city', sa.String(length=100), nullable=False),
            sa.Column('state', sa.String(length=100), nullable=False),
            sa.Column('postal_code', sa.String(length=20), nullable=False),
            sa.Column('latitude', sa.Float(), nullable=True),
            sa.Column('longitude', sa.Float(), nullable=True),
            sa.Column('serviceability_status', sa.String(length=32), nullable=False, server_default='PENDING_SURVEY'),
            sa.Column('candidate_node_id', sa.Integer(), sa.ForeignKey('nodes.id', ondelete='SET NULL'), nullable=True),
            sa.Column('distance_to_node_meters', sa.Float(), nullable=True),
            sa.Column('is_archived', sa.Boolean(), nullable=False, server_default=sa.text('0' if bind.dialect.name == 'sqlite' else 'false')),
            sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint(
                "address_type IN ('INSTALLATION', 'BILLING', 'REGISTERED')",
                name='ck_crm_address_type'
            ),
            sa.CheckConstraint(
                "serviceability_status IN ('PENDING_SURVEY', 'FEASIBLE', 'SERVICEABLE', 'UNSERVICEABLE')",
                name='ck_crm_address_serviceability'
            ),
        )
        op.create_index('ix_crm_addresses_crm_customer_id', 'crm_addresses', ['crm_customer_id'])
        op.create_index('ix_crm_addresses_postal_code', 'crm_addresses', ['postal_code'])

    # 4. Create 'crm_documents' table (ondelete='RESTRICT' protects KYC compliance audit)
    if 'crm_documents' not in existing_tables:
        op.create_table(
            'crm_documents',
            sa.Column('id', sa.CHAR(36), nullable=False, primary_key=True),
            sa.Column('crm_customer_id', sa.CHAR(36), sa.ForeignKey('crm_customers.id', ondelete='RESTRICT'), nullable=False),
            sa.Column('document_type', sa.String(length=64), nullable=False),
            sa.Column('document_identifier', sa.String(length=64), nullable=True),
            sa.Column('file_key', sa.String(length=512), nullable=True),
            sa.Column('upload_status', sa.String(length=32), nullable=False, server_default='PENDING_UPLOAD'),
            sa.Column('file_name', sa.String(length=255), nullable=False),
            sa.Column('file_size_bytes', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('mime_type', sa.String(length=128), nullable=False),
            sa.Column('verification_status', sa.String(length=32), nullable=False, server_default='PENDING'),
            sa.Column('verified_by_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
            sa.Column('verified_at', sa.DateTime(), nullable=True),
            sa.Column('rejection_reason', sa.Text(), nullable=True),
            sa.Column('is_archived', sa.Boolean(), nullable=False, server_default=sa.text('0' if bind.dialect.name == 'sqlite' else 'false')),
            sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint(
                "document_type IN ('AADHAAR_MASKED', 'PAN', 'INCORPORATION_CERT', 'LEASE_AGREEMENT', 'VOTER_ID', 'PASSPORT', 'DRIVING_LICENSE')",
                name='ck_crm_document_type'
            ),
            sa.CheckConstraint(
                "upload_status IN ('PENDING_UPLOAD', 'UPLOADED', 'FAILED', 'ARCHIVED')",
                name='ck_crm_document_upload_status'
            ),
            sa.CheckConstraint(
                "verification_status IN ('PENDING', 'VERIFIED', 'REJECTED')",
                name='ck_crm_document_verification_status'
            ),
        )
        op.create_index('ix_crm_documents_crm_customer_id', 'crm_documents', ['crm_customer_id'])
        op.create_index('ix_crm_documents_upload_status', 'crm_documents', ['upload_status'])
        op.create_index('ix_crm_documents_verification_status', 'crm_documents', ['verification_status'])

    # 5. Create 'crm_account_users' table
    if 'crm_account_users' not in existing_tables:
        op.create_table(
            'crm_account_users',
            sa.Column('id', sa.CHAR(36), nullable=False, primary_key=True),
            sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
            sa.Column('crm_customer_id', sa.CHAR(36), sa.ForeignKey('crm_customers.id', ondelete='CASCADE'), nullable=False),
            sa.Column('portal_role', sa.String(length=32), nullable=False, server_default='ACCOUNT_VIEWER'),
            sa.Column('verification_status', sa.String(length=32), nullable=False, server_default='PENDING'),
            sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('1' if bind.dialect.name == 'sqlite' else 'true')),
            sa.Column('verified_by_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
            sa.Column('verified_at', sa.DateTime(), nullable=True),
            sa.Column('revocation_reason', sa.Text(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint('user_id', 'crm_customer_id', name='uq_crm_account_user'),
            sa.CheckConstraint(
                "portal_role IN ('ACCOUNT_OWNER', 'ACCOUNT_ADMIN', 'ACCOUNT_BILLING', 'ACCOUNT_VIEWER')",
                name='ck_crm_account_user_portal_role'
            ),
            sa.CheckConstraint(
                "verification_status IN ('PENDING', 'VERIFIED', 'REJECTED', 'REVOKED')",
                name='ck_crm_account_user_verification_status'
            ),
        )
        op.create_index('ix_crm_account_users_user_id', 'crm_account_users', ['user_id'])
        op.create_index('ix_crm_account_users_crm_customer_id', 'crm_account_users', ['crm_customer_id'])

    # 6. Create 'crm_activities' table (ondelete='RESTRICT' protects compliance audit trail)
    if 'crm_activities' not in existing_tables:
        op.create_table(
            'crm_activities',
            sa.Column('id', sa.CHAR(36), nullable=False, primary_key=True),
            sa.Column('crm_customer_id', sa.CHAR(36), sa.ForeignKey('crm_customers.id', ondelete='RESTRICT'), nullable=False),
            sa.Column('activity_type', sa.String(length=64), nullable=False),
            sa.Column('summary', sa.String(length=255), nullable=False),
            sa.Column('details', sa.Text(), nullable=True),
            sa.Column('performed_by_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint(
                "activity_type IN ('NOTE', 'STATUS_CHANGE', 'CONTACT_ADDED', 'ADDRESS_UPDATED', 'KYC_VERIFIED', 'MIGRATION_BACKFILL', 'SYSTEM')",
                name='ck_crm_activity_type'
            ),
        )
        op.create_index('ix_crm_activities_crm_customer_id', 'crm_activities', ['crm_customer_id'])
        op.create_index('ix_crm_activities_activity_type', 'crm_activities', ['activity_type'])


def downgrade() -> None:
    bind = op.get_bind()
    context = op.get_context()
    if getattr(context, 'as_sql', False) or 'MockConnection' in type(bind).__name__:
        existing_tables = {'crm_activities', 'crm_account_users', 'crm_documents', 'crm_addresses', 'crm_contacts', 'crm_customers'}
    else:
        inspector = sa.inspect(bind)
        existing_tables = set(inspector.get_table_names())

    # Drop tables in reverse dependency order
    if 'crm_activities' in existing_tables:
        op.drop_table('crm_activities')
    if 'crm_account_users' in existing_tables:
        op.drop_table('crm_account_users')
    if 'crm_documents' in existing_tables:
        op.drop_table('crm_documents')
    if 'crm_addresses' in existing_tables:
        op.drop_table('crm_addresses')
    if 'crm_contacts' in existing_tables:
        try:
            op.drop_index('uq_crm_contacts_customer_primary', table_name='crm_contacts')
        except Exception:
            pass
        op.drop_table('crm_contacts')
    if 'crm_customers' in existing_tables:
        op.drop_table('crm_customers')
