from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import exists, func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.future import select

from api.db.base_client import BaseDBClient
from api.db.models import (
    APIKeyModel,
    OrganizationModel,
    UserModel,
    organization_users_association,
)
from api.utils.api_key import generate_api_key


class OrganizationClient(BaseDBClient):
    async def list_organizations_for_superadmin(self) -> list[dict]:
        """List every organization with its member count for the superadmin panel.

        Returns newest-first. Not organization-scoped by design — this is only
        reachable behind the superuser dependency.
        """
        # Left join the association table so orgs with zero members still appear.
        user_count = func.count(organization_users_association.c.user_id)
        async with self.async_session() as session:
            result = await session.execute(
                select(OrganizationModel, user_count)
                .outerjoin(
                    organization_users_association,
                    organization_users_association.c.organization_id
                    == OrganizationModel.id,
                )
                .group_by(OrganizationModel.id)
                .order_by(OrganizationModel.created_at.desc())
            )
            return [
                {
                    "id": org.id,
                    "provider_id": org.provider_id,
                    "name": org.name,
                    "primary_contact_email": org.primary_contact_email,
                    "status": org.status,
                    "created_at": org.created_at,
                    "user_count": int(count or 0),
                }
                for org, count in result.all()
            ]

    async def create_client_organization(
        self,
        *,
        provider_id: str,
        name: str,
        primary_contact_email: str,
        superadmin_user_id: int,
    ) -> OrganizationModel:
        """Create a superadmin-provisioned client organization.

        Inserts the org as ``pending_setup``, adds the creating superadmin as a
        member (so they can build the client's workflows before the client
        accepts their invite) and mints the org's default API key. Mirrors the
        side effects of get_or_create_organization_by_provider_id.
        """
        async with self.async_session() as session:
            organization = OrganizationModel(
                provider_id=provider_id,
                name=name,
                primary_contact_email=primary_contact_email,
                status="pending_setup",
                created_at=datetime.now(timezone.utc),
            )
            session.add(organization)
            await session.commit()
            await session.refresh(organization)

            # Add the superadmin as a member (idempotent).
            stmt = insert(organization_users_association).values(
                user_id=superadmin_user_id, organization_id=organization.id
            )
            stmt = stmt.on_conflict_do_nothing()
            await session.execute(stmt)

            # Default API key, same as auto-provisioned orgs get.
            _, key_hash, key_prefix = generate_api_key()
            session.add(
                APIKeyModel(
                    organization_id=organization.id,
                    name="Default API Key",
                    key_hash=key_hash,
                    key_prefix=key_prefix,
                    is_active=True,
                    created_by=superadmin_user_id,
                )
            )
            await session.commit()
            await session.refresh(organization)
            return organization

    async def get_organization_by_provider_id(
        self, provider_id: str
    ) -> Optional[OrganizationModel]:
        """Get an organization by its Stack team provider_id."""
        async with self.async_session() as session:
            result = await session.execute(
                select(OrganizationModel).where(
                    OrganizationModel.provider_id == provider_id
                )
            )
            return result.scalars().first()

    async def update_organization_status(
        self, organization_id: int, status: str
    ) -> Optional[OrganizationModel]:
        """Set an organization's lifecycle status. Returns the updated row."""
        async with self.async_session() as session:
            result = await session.execute(
                select(OrganizationModel).where(OrganizationModel.id == organization_id)
            )
            organization = result.scalars().first()
            if organization is None:
                return None
            organization.status = status
            await session.commit()
            await session.refresh(organization)
            return organization

    async def get_organization_by_id(
        self, organization_id: int
    ) -> Optional[OrganizationModel]:
        """Get an organization by its ID."""
        async with self.async_session() as session:
            result = await session.execute(
                select(OrganizationModel).where(OrganizationModel.id == organization_id)
            )
            return result.scalars().first()

    async def get_organization_users(self, organization_id: int) -> list[UserModel]:
        """Get all users linked to an organization (many-to-many)."""
        async with self.async_session() as session:
            result = await session.execute(
                select(UserModel)
                .join(
                    organization_users_association,
                    organization_users_association.c.user_id == UserModel.id,
                )
                .where(
                    organization_users_association.c.organization_id == organization_id
                )
                .order_by(UserModel.id)
            )
            return list(result.scalars().all())

    async def get_or_create_organization_by_provider_id(
        self, org_provider_id: str, user_id: int
    ) -> tuple[OrganizationModel, bool]:
        """Get an existing organization by provider_id or create a new one.

        Returns:
            A tuple of (organization, was_created) where was_created is True if the organization
            was created in this call, False if it already existed.
        """
        async with self.async_session() as session:
            # First try to get existing organization
            result = await session.execute(
                select(OrganizationModel).where(
                    OrganizationModel.provider_id == org_provider_id
                )
            )
            organization = result.scalars().first()

            if organization is None:
                # Use PostgreSQL's INSERT ... ON CONFLICT DO NOTHING
                # This is atomic and handles race conditions at the database level

                stmt = insert(OrganizationModel.__table__).values(
                    provider_id=org_provider_id, created_at=datetime.now(timezone.utc)
                )
                # ON CONFLICT DO NOTHING - if another request already inserted, this becomes a no-op
                stmt = stmt.on_conflict_do_nothing(index_elements=["provider_id"])

                result = await session.execute(stmt)
                await session.commit()

                # Check if we actually inserted (rowcount > 0) or if there was a conflict (rowcount == 0)
                was_created = result.rowcount > 0

                # Now fetch the organization (either the one we just created or the one that existed)
                result = await session.execute(
                    select(OrganizationModel).where(
                        OrganizationModel.provider_id == org_provider_id
                    )
                )
                organization = result.scalars().first()

                if organization is None:
                    # This should never happen, but handle it just in case
                    error_msg = f"Failed to create or fetch organization with provider_id {org_provider_id}"
                    raise ValueError(error_msg)

                # Only create API key if we actually created the organization
                if was_created:
                    # Create a default API key for the new organization
                    _, key_hash, key_prefix = generate_api_key()

                    api_key = APIKeyModel(
                        organization_id=organization.id,
                        name="Default API Key",
                        key_hash=key_hash,
                        key_prefix=key_prefix,
                        is_active=True,
                        created_by=user_id,
                    )
                    session.add(api_key)
                    await session.commit()

                await session.refresh(organization)
                return organization, was_created
            return organization, False

    async def is_user_member_of_organization(
        self, user_id: int, organization_id: int
    ) -> bool:
        """Return True if the user belongs to the given organization."""
        async with self.async_session() as session:
            result = await session.execute(
                select(
                    exists().where(
                        (organization_users_association.c.user_id == user_id)
                        & (
                            organization_users_association.c.organization_id
                            == organization_id
                        )
                    )
                )
            )
            return bool(result.scalar())

    async def add_user_to_organization(
        self, user_id: int, organization_id: int
    ) -> None:
        """Ensure that a user is linked to an organization (many-to-many).

        The association is created only if it does not already exist.
        Uses INSERT ... ON CONFLICT DO NOTHING to handle race conditions.
        """
        async with self.async_session() as session:
            # Use PostgreSQL's INSERT ... ON CONFLICT DO NOTHING
            # This handles race conditions at the database level

            stmt = insert(organization_users_association).values(
                user_id=user_id, organization_id=organization_id
            )
            # ON CONFLICT DO NOTHING - if another request already inserted, this becomes a no-op
            # The primary key constraint on (user_id, organization_id) will trigger the conflict
            stmt = stmt.on_conflict_do_nothing()

            await session.execute(stmt)
            await session.commit()
