"""经营设置：business_settings（退款手续费、储值扣减顺序、退券策略、补卡费等商户日常调整的经营规则，后台维护）

这几项原来在运行配置文件里（[wallet] split_policy、customer_refund_fee_bp，[coupon] return_policy、return_window_hours，
[member_card] replace_fee_cents）；改为后台维护后，没有行时用各模块登记的默认值（与原配置文件的默认值相同），
项目若改过配置文件里的取值，上线后在后台“经营设置”页填一次。

Revision ID: 0004_business_settings
Revises: 0003_purchase_quotas
Create Date: 2026-10-30 12:00:00+00:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '0004_business_settings'
down_revision = '0003_purchase_quotas'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('business_settings',
    sa.Column('store_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=True),
    sa.Column('key', sa.String(length=64), nullable=False),
    sa.Column('value_json', sa.Text(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('updated_by', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=True),
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_business_settings'))
    )
    with op.batch_alter_table('business_settings', schema=None) as batch_op:
        batch_op.create_index('uq_business_settings_global_key', ['key'], unique=True, sqlite_where=sa.text('store_id IS NULL'), postgresql_where=sa.text('store_id IS NULL'))
        batch_op.create_index('uq_business_settings_store_key', ['store_id', 'key'], unique=True, sqlite_where=sa.text('store_id IS NOT NULL'), postgresql_where=sa.text('store_id IS NOT NULL'))


def downgrade() -> None:
    with op.batch_alter_table('business_settings', schema=None) as batch_op:
        batch_op.drop_index('uq_business_settings_store_key', sqlite_where=sa.text('store_id IS NOT NULL'), postgresql_where=sa.text('store_id IS NOT NULL'))
        batch_op.drop_index('uq_business_settings_global_key', sqlite_where=sa.text('store_id IS NULL'), postgresql_where=sa.text('store_id IS NULL'))
    op.drop_table('business_settings')
