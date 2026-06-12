"""orders.broker_account_id: CASCADE -> SET NULL (stop trade-history loss)

Disconnecting a broker account was CASCADE-deleting every order (and its
fills) placed on it — silently wiping the user's trade and P&L history.
The brokers API docstring always claimed "Order rows survive (SET NULL)";
this migration makes the schema match: the column becomes nullable and the
FK is recreated with ON DELETE SET NULL.

Defensive by design: the original FK was created UNNAMED in the initial
schema migration, so its server-side name is whatever Postgres generated.
Rather than hardcode a guessed name (which aborts the whole boot when it
doesn't match — alembic runs in the backend CMD before uvicorn), we look up
and drop every FK on orders.broker_account_id from pg_constraint, then
recreate one with a known name. Every step is idempotent.

Revision ID: b7c8d9e0f1a2
Revises: a1b2c3d4e5f6
Create Date: 2026-06-12 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op


revision: str = "b7c8d9e0f1a2"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_FK_NAME = "orders_broker_account_id_fkey"

_DROP_ALL_FKS_ON_COLUMN = """
DO $$
DECLARE r RECORD;
BEGIN
  FOR r IN
    SELECT con.conname
      FROM pg_constraint con
      JOIN pg_class rel ON rel.oid = con.conrelid
     WHERE rel.relname = 'orders'
       AND con.contype = 'f'
       AND EXISTS (
         SELECT 1 FROM pg_attribute att
          WHERE att.attrelid = rel.oid
            AND att.attnum = ANY (con.conkey)
            AND att.attname = 'broker_account_id'
       )
  LOOP
    EXECUTE format('ALTER TABLE orders DROP CONSTRAINT %I', r.conname);
  END LOOP;
END $$;
"""


def upgrade() -> None:
    # 1. Column becomes nullable (DROP NOT NULL is a no-op if already nullable).
    op.execute("ALTER TABLE orders ALTER COLUMN broker_account_id DROP NOT NULL")
    # 2. Drop the existing FK(s) on the column, whatever they're named.
    op.execute(_DROP_ALL_FKS_ON_COLUMN)
    # 3. Recreate under a known name with SET NULL semantics.
    op.create_foreign_key(
        _FK_NAME, "orders", "broker_accounts",
        ["broker_account_id"], ["id"], ondelete="SET NULL",
    )


def downgrade() -> None:
    op.execute(_DROP_ALL_FKS_ON_COLUMN)
    # Orphaned rows (NULL) block restoring NOT NULL; delete them first —
    # this mirrors what CASCADE would have done at disconnect time.
    op.execute("DELETE FROM orders WHERE broker_account_id IS NULL")
    op.create_foreign_key(
        _FK_NAME, "orders", "broker_accounts",
        ["broker_account_id"], ["id"], ondelete="CASCADE",
    )
    op.execute("ALTER TABLE orders ALTER COLUMN broker_account_id SET NOT NULL")
