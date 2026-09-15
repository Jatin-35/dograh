"""add length to code editor env vars

A stored variable's plaintext length is not sensitive — revealing it is the
whole point of this migration — but nothing before it was ever persisted, so
the UI could only mask a value with a fixed, made-up number of placeholder
characters. This lets it mask with exactly as many characters as the value
actually has.

Nullable and never backfilled: a row written before this column existed has
no plaintext to measure anymore (only a Fernet ciphertext, whose length does
not map cleanly back to the original), so old rows fall back to a fixed mask
in the UI rather than a wrong one.

Revision ID: b4f8e21c6a97
Revises: e6b2d4f70a13
"""

import sqlalchemy as sa
from alembic import op

revision = "b4f8e21c6a97"
down_revision = "e6b2d4f70a13"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "code_editor_env_vars",
        sa.Column("value_length", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("code_editor_env_vars", "value_length")
