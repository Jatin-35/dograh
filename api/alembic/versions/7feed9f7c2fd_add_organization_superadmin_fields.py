"""add superadmin-managed fields to organizations (name, contact email, status)

Revision ID: 7feed9f7c2fd
Revises: 00b0201ad918
Create Date: 2026-07-23 13:30:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7feed9f7c2fd"
down_revision: Union[str, None] = "00b0201ad918"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create the organization_status enum before adding the column that
    # references it. Alembic does not emit CREATE TYPE for Enum objects used in
    # op.add_column(), so Postgres would otherwise raise
    # `type "organization_status" does not exist`.
    organization_status_enum = sa.Enum(
        "pending_setup",
        "active",
        "suspended",
        name="organization_status",
    )
    organization_status_enum.create(op.get_bind(), checkfirst=True)

    # Human-readable client name and contact email, entered by a superadmin when
    # creating a client org. Nullable so pre-existing / auto-provisioned orgs
    # (which have no entered name) keep working.
    op.add_column("organizations", sa.Column("name", sa.String(), nullable=True))
    op.add_column(
        "organizations",
        sa.Column("primary_contact_email", sa.String(), nullable=True),
    )

    # Existing rows already have logged-in users, so default them to 'active'.
    # Superadmin-created orgs will explicitly be inserted as 'pending_setup'.
    op.add_column(
        "organizations",
        sa.Column(
            "status",
            organization_status_enum,
            nullable=False,
            server_default="active",
        ),
    )


def downgrade() -> None:
    op.drop_column("organizations", "status")
    op.drop_column("organizations", "primary_contact_email")
    op.drop_column("organizations", "name")

    sa.Enum(name="organization_status").drop(op.get_bind(), checkfirst=True)
