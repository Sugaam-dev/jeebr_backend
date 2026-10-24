"""add_user_market_assignments_table

Revision ID: i9c0d1e2f3a4
Revises: h8b9c0d1e2f3
Create Date: 2026-10-25 00:50:00.000000

SentinelOS Market Authorization Domain Migration:
Creates persistent user_market_assignments table and safely backfills
existing users from operational assignments, resource bindings, and customer records.
Preserves Super Admin global access and prevents unassigned users from receiving silent default assignments.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'i9c0d1e2f3a4'
down_revision: Union[str, Sequence[str], None] = 'h8b9c0d1e2f3'
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

    if 'user_market_assignments' not in existing_tables:
        op.create_table(
            'user_market_assignments',
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
            sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
            sa.Column('market_id', sa.String(length=50), nullable=False),
            sa.Column('is_primary', sa.Boolean(), nullable=False, server_default=sa.text('0' if bind.dialect.name == 'sqlite' else 'false')),
            sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column('created_by_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
            sa.UniqueConstraint('user_id', 'market_id', name='uq_user_market')
        )
        op.create_index('ix_user_market_assignments_id', 'user_market_assignments', ['id'])
        op.create_index('ix_user_market_assignments_user_id', 'user_market_assignments', ['user_id'])
        op.create_index('ix_user_market_assignments_market_id', 'user_market_assignments', ['market_id'])

    # Data Backfill: Populate user_market_assignments for existing users
    if not (getattr(context, 'as_sql', False) or 'MockConnection' in type(bind).__name__):
        CANONICAL_SEEDED_ASSIGNMENTS = {
            "noc@pmrg.in": {"markets": ["mumbai", "kolkata"], "primary": "mumbai"},
            "care@pmrg.in": {"markets": ["mumbai"], "primary": "mumbai"},
            "revenue@pmrg.in": {"markets": ["mumbai"], "primary": "mumbai"},
            "executive@pmrg.in": {"markets": ["mumbai", "kolkata"], "primary": "mumbai"},
            "client@pmrgsolution.com": {"markets": ["mumbai"], "primary": "mumbai"},
            "admin@pmrg.in": {"markets": ["mumbai", "kolkata"], "primary": "mumbai"},
            "admin_prov_test@pmrg.in": {"markets": ["mumbai", "kolkata"], "primary": "mumbai"},
            "admin@test.sentinel": {"markets": ["mumbai", "kolkata"], "primary": "mumbai"},
            "noc@test.sentinel": {"markets": ["mumbai", "kolkata"], "primary": "mumbai"},
        }
        VALID_MARKETS = {"mumbai", "kolkata"}

        users_table = sa.table(
            'users',
            sa.column('id', sa.Integer),
            sa.column('email', sa.String),
            sa.column('role', sa.String)
        )
        assignments_table = sa.table(
            'user_market_assignments',
            sa.column('user_id', sa.Integer),
            sa.column('market_id', sa.String),
            sa.column('is_primary', sa.Boolean)
        )
        resources_table = sa.table(
            'resources',
            sa.column('user_id', sa.Integer),
            sa.column('email', sa.String),
            sa.column('market_id', sa.String)
        )
        customers_table = sa.table(
            'customers',
            sa.column('email', sa.String),
            sa.column('market_id', sa.String)
        )

        users = bind.execute(sa.select(users_table.c.id, users_table.c.email, users_table.c.role)).fetchall()
        
        current_rows = bind.execute(sa.select(assignments_table.c.user_id, assignments_table.c.market_id)).fetchall()
        existing_user_markets = set()
        for uid, mid in current_rows:
            existing_user_markets.add((uid, mid.lower().strip()))

        for u_id, u_email, u_role in users:
            email_lower = (u_email or "").lower().strip()

            # 1. Match from CANONICAL_SEEDED_ASSIGNMENTS
            if email_lower in CANONICAL_SEEDED_ASSIGNMENTS:
                cfg = CANONICAL_SEEDED_ASSIGNMENTS[email_lower]
                markets = cfg.get("markets", [])
                primary = cfg.get("primary")
                for m in markets:
                    m_clean = m.lower().strip()
                    if m_clean in VALID_MARKETS and (u_id, m_clean) not in existing_user_markets:
                        bind.execute(
                            assignments_table.insert().values(
                                user_id=u_id,
                                market_id=m_clean,
                                is_primary=(m_clean == primary)
                            )
                        )
                        existing_user_markets.add((u_id, m_clean))
                continue

            # 2. Match from resources table (Field Engineers)
            res_rows = bind.execute(
                sa.select(resources_table.c.market_id).where(
                    sa.or_(
                        resources_table.c.user_id == u_id,
                        sa.func.lower(resources_table.c.email) == email_lower
                    )
                )
            ).fetchall()
            if res_rows:
                res_markets = list(dict.fromkeys(
                    r[0].lower().strip() for r in res_rows
                    if r[0] and r[0].lower().strip() in VALID_MARKETS
                ))
                for idx, m_clean in enumerate(res_markets):
                    if (u_id, m_clean) not in existing_user_markets:
                        bind.execute(
                            assignments_table.insert().values(
                                user_id=u_id,
                                market_id=m_clean,
                                is_primary=(idx == 0 and len(res_markets) == 1)
                            )
                        )
                        existing_user_markets.add((u_id, m_clean))
                if res_markets:
                    continue

            # 3. Match from customers table (Customer subscribers)
            cust_rows = bind.execute(
                sa.select(customers_table.c.market_id).where(
                    sa.func.lower(customers_table.c.email) == email_lower
                )
            ).fetchall()
            if cust_rows:
                for r in cust_rows:
                    m_clean = (r[0] or "").lower().strip()
                    if m_clean in VALID_MARKETS and (u_id, m_clean) not in existing_user_markets:
                        bind.execute(
                            assignments_table.insert().values(
                                user_id=u_id,
                                market_id=m_clean,
                                is_primary=True
                            )
                        )
                        existing_user_markets.add((u_id, m_clean))
                        break


def downgrade() -> None:
    op.drop_table('user_market_assignments')
