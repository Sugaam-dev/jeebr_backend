"""add_rbac_and_workload_architecture

Revision ID: bfe079a19047
Revises: c3d4e5f6a7b8
Create Date: 2026-09-28 10:57:26.347704

"""
from typing import Sequence, Union
from datetime import datetime
from alembic import op
import sqlalchemy as sa
from sqlalchemy.sql import table, column


# revision identifiers, used by Alembic.
revision: str = 'bfe079a19047'
down_revision: Union[str, Sequence[str], None] = 'c3d4e5f6a7b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Create 'roles' table
    op.create_table(
        'roles',
        sa.Column('id', sa.Integer(), primary_key=True, index=True),
        sa.Column('name', sa.String(length=50), nullable=False, unique=True),
        sa.Column('display_name', sa.String(length=100), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('rank', sa.Integer(), nullable=False, server_default='50'),
        sa.Column('is_system', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now())
    )
    op.create_index('ix_roles_name', 'roles', ['name'])

    # 2. Create 'permissions' table
    op.create_table(
        'permissions',
        sa.Column('id', sa.Integer(), primary_key=True, index=True),
        sa.Column('code', sa.String(length=100), nullable=False, unique=True),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('category', sa.String(length=50), nullable=False),
        sa.Column('description', sa.Text(), nullable=True)
    )
    op.create_index('ix_permissions_code', 'permissions', ['code'])

    # 3. Create 'role_permissions' table
    op.create_table(
        'role_permissions',
        sa.Column('id', sa.Integer(), primary_key=True, index=True),
        sa.Column('role_id', sa.Integer(), sa.ForeignKey('roles.id', ondelete='CASCADE'), nullable=False),
        sa.Column('permission_code', sa.String(length=100), sa.ForeignKey('permissions.code', ondelete='CASCADE'), nullable=False)
    )
    op.create_index('ix_role_permissions_role_id', 'role_permissions', ['role_id'])
    op.create_index('ix_role_permissions_permission_code', 'role_permissions', ['permission_code'])

    # 4. Seed initial RBAC metadata directly in migration
    from app.services.rbac_service import PERMISSION_REGISTRY, SYSTEM_ROLES
    from app.auth import hash_password

    bind = op.get_bind()
    from sqlalchemy.orm import Session
    db = Session(bind=bind)

    try:
        from app.models import Role, Permission, RolePermission, User
        # Seed permissions
        for p in PERMISSION_REGISTRY:
            existing = db.query(Permission).filter(Permission.code == p["code"]).first()
            if not existing:
                db.add(Permission(
                    code=p["code"],
                    name=p["name"],
                    category=p["category"],
                    description=p["description"]
                ))
        db.flush()

        # Seed roles
        role_objs = {}
        for r_name, r_def in SYSTEM_ROLES.items():
            existing_r = db.query(Role).filter(Role.name == r_name).first()
            if not existing_r:
                existing_r = Role(
                    name=r_name,
                    display_name=r_def["display_name"],
                    description=r_def["description"],
                    rank=r_def["rank"],
                    is_system=True
                )
                db.add(existing_r)
                db.flush()
            role_objs[r_name] = existing_r

        # Seed role permissions
        for r_name, r_def in SYSTEM_ROLES.items():
            r = role_objs[r_name]
            for p_code in r_def["permissions"]:
                existing_rp = db.query(RolePermission).filter(
                    RolePermission.role_id == r.id,
                    RolePermission.permission_code == p_code
                ).first()
                if not existing_rp:
                    db.add(RolePermission(role_id=r.id, permission_code=p_code))

        # 5. Provision bootstrap Super Admin user
        super_admin = db.query(User).filter(User.email == "superadmin@pmrg.in").first()
        if not super_admin:
            db.add(User(
                email="superadmin@pmrg.in",
                hashed_password=hash_password("admin123"),
                full_name="Sentinel Super Administrator",
                role="SUPER_ADMIN",
                is_active=True
            ))

        db.commit()
    except Exception as e:
        db.rollback()
        raise e


def downgrade() -> None:
    op.drop_table('role_permissions')
    op.drop_table('permissions')
    op.drop_table('roles')
