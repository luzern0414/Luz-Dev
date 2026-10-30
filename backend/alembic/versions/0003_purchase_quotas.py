"""购买规则计数：order_purchase_counters → order_purchase_quotas（按主体与计数键，含件数、单数与周期结束时刻）

旧表只有“每人不分周期的件数”，按 subject=user:<会员>、counter_key=<kind:product_id>|all|all 迁入新表；
旧订单行快照里的 limit_per_user 由 order.purchase.item_quotas 按同一口径还原，取消时照常退回。

Revision ID: 0003_purchase_quotas
Revises: 0002_config_content
Create Date: 2026-10-30 09:00:00+00:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '0003_purchase_quotas'
down_revision = '0002_config_content'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('order_purchase_quotas',
    sa.Column('subject', sa.String(length=80), nullable=False),
    sa.Column('counter_key', sa.String(length=160), nullable=False),
    sa.Column('qty', sa.Integer(), nullable=False),
    sa.Column('orders', sa.Integer(), nullable=False),
    sa.Column('window_end_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_order_purchase_quotas')),
    sa.UniqueConstraint('subject', 'counter_key', name='uq_order_purchase_quotas_subject_key')
    )
    with op.batch_alter_table('order_purchase_quotas', schema=None) as batch_op:
        batch_op.create_index('ix_order_purchase_quotas_window_end', ['window_end_at'], unique=False)
    op.execute(
        "INSERT INTO order_purchase_quotas (subject, counter_key, qty, orders, window_end_at, updated_at) "
        "SELECT 'user:' || CAST(user_id AS VARCHAR), product_key || '|all|all', qty, 0, NULL, updated_at "
        "FROM order_purchase_counters"
    )
    op.drop_table('order_purchase_counters')


def downgrade() -> None:
    op.create_table('order_purchase_counters',
    sa.Column('user_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('product_key', sa.String(length=64), nullable=False),
    sa.Column('qty', sa.Integer(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_order_purchase_counters')),
    sa.UniqueConstraint('user_id', 'product_key', name='uq_order_purchase_counters_user_product')
    )
    # 只能还原“按会员、不分周期、全部门店”的件数；周期、门店与手机号维度的计数在旧表里没有位置，丢弃。
    op.execute(
        "INSERT INTO order_purchase_counters (user_id, product_key, qty, updated_at) "
        "SELECT CAST(SUBSTR(subject, 6) AS BIGINT), SUBSTR(counter_key, 1, LENGTH(counter_key) - 8), qty, updated_at "
        "FROM order_purchase_quotas WHERE subject LIKE 'user:%' AND counter_key LIKE '%|all|all' "
        "AND LENGTH(counter_key) - 8 <= 64"
    )
    with op.batch_alter_table('order_purchase_quotas', schema=None) as batch_op:
        batch_op.drop_index('ix_order_purchase_quotas_window_end')
    op.drop_table('order_purchase_quotas')
