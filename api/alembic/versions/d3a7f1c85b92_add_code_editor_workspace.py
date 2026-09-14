"""add code editor workspace

Per-organization virtual filesystem, immutable version snapshots, and encrypted
environment variables for the Code Editor.

down_revision is deliberately b7e4c2a91f58 — the last *committed* revision, and
therefore the one production is actually at. `alembic heads` will happily report
a local head belonging to an unpushed branch; chaining onto one of those
produces a migration production cannot resolve, which has taken this deployment
down before. Always chain onto what is committed, and prefer
`alembic upgrade heads` over `head` so a second branch does not stall the
upgrade.

Revision ID: d3a7f1c85b92
Revises: b7e4c2a91f58
"""

import sqlalchemy as sa
from alembic import op

revision = "d3a7f1c85b92"
down_revision = "b7e4c2a91f58"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "code_editor_files",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("path", sa.String(length=512), nullable=False),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_by", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id", "path", name="uq_code_editor_files_org_path"
        ),
    )
    op.create_index(
        "ix_code_editor_files_org", "code_editor_files", ["organization_id"]
    )
    op.create_index(
        op.f("ix_code_editor_files_id"), "code_editor_files", ["id"]
    )

    op.create_table(
        "code_editor_versions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("files", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("deployed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id", "version_number", name="uq_code_editor_versions_org_num"
        ),
    )
    op.create_index(
        "ix_code_editor_versions_org", "code_editor_versions", ["organization_id"]
    )
    op.create_index(
        "ix_code_editor_versions_deployed",
        "code_editor_versions",
        ["organization_id", "deployed_at"],
    )
    op.create_index(
        op.f("ix_code_editor_versions_id"), "code_editor_versions", ["id"]
    )

    op.create_table(
        "code_editor_env_vars",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("value_encrypted", sa.Text(), nullable=False),
        sa.Column("value_hint", sa.String(length=8), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id", "key", name="uq_code_editor_env_vars_org_key"
        ),
    )
    op.create_index(
        "ix_code_editor_env_vars_org", "code_editor_env_vars", ["organization_id"]
    )
    op.create_index(
        op.f("ix_code_editor_env_vars_id"), "code_editor_env_vars", ["id"]
    )


def downgrade() -> None:
    op.drop_table("code_editor_env_vars")
    op.drop_table("code_editor_versions")
    op.drop_table("code_editor_files")
