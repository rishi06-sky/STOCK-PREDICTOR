"""rename yahoo_suffix to exchange_suffix

The column never was Yahoo-specific: it holds the canonical exchange
decoration (".NS", ".BO") that every provider translates from. Under the old
name Security.provider_symbol applied it for Yahoo alone, so Stooq and Alpha
Vantage received bare tickers and resolved them against the wrong market.

Index securities also move from a {"yahoo": ...} override to {"*": ...}, which
pins one spelling across providers now that the suffix is applied to all.

Revision ID: b7a949c95832
Revises: bfe5f434b4fe
Create Date: 2026-09-15 11:05:10.687215
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = 'b7a949c95832'
down_revision = 'bfe5f434b4fe'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("exchanges", "yahoo_suffix", new_column_name="exchange_suffix")
    # Re-key the per-security overrides: {"yahoo": X} -> {"*": X}. Only rows
    # whose sole key is "yahoo" are rewritten, so a genuinely Yahoo-specific
    # override alongside others is left alone.
    op.execute(
        """
        UPDATE securities
           SET provider_symbols =
               jsonb_build_object('*', provider_symbols -> 'yahoo')
         WHERE provider_symbols ? 'yahoo'
           AND (SELECT count(*) FROM jsonb_object_keys(provider_symbols)) = 1
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE securities
           SET provider_symbols =
               jsonb_build_object('yahoo', provider_symbols -> '*')
         WHERE provider_symbols ? '*'
           AND (SELECT count(*) FROM jsonb_object_keys(provider_symbols)) = 1
        """
    )
    op.alter_column("exchanges", "exchange_suffix", new_column_name="yahoo_suffix")
