"""Storage for Code Editor workspaces, versions and environment variables.

Every method is organization-scoped. A workspace is one org's code, and code is
about the most sensitive thing this platform stores on their behalf — so there
is deliberately no method here that reads a file without an organization_id.
"""

from datetime import UTC, datetime
from typing import Optional

from sqlalchemy import func, select, text

from api.db.base_client import BaseDBClient
from api.db.models import (
    CodeEditorEnvVarModel,
    CodeEditorFileModel,
    CodeEditorVersionModel,
)


class CodeEditorClient(BaseDBClient):
    # ------------------------------------------------------------------
    # Files (the working draft)
    # ------------------------------------------------------------------

    async def list_code_editor_files(
        self, organization_id: int
    ) -> list[CodeEditorFileModel]:
        async with self.async_session() as session:
            result = await session.execute(
                select(CodeEditorFileModel)
                .where(CodeEditorFileModel.organization_id == organization_id)
                .order_by(CodeEditorFileModel.path)
            )
            return list(result.scalars().all())

    async def get_code_editor_file(
        self, organization_id: int, path: str
    ) -> Optional[CodeEditorFileModel]:
        async with self.async_session() as session:
            result = await session.execute(
                select(CodeEditorFileModel).where(
                    CodeEditorFileModel.organization_id == organization_id,
                    CodeEditorFileModel.path == path,
                )
            )
            return result.scalars().first()

    async def get_code_editor_workspace(self, organization_id: int) -> dict[str, str]:
        """The whole draft as {path: content} — what the sandbox executes."""
        files = await self.list_code_editor_files(organization_id)
        return {f.path: f.content for f in files}

    async def upsert_code_editor_file(
        self,
        organization_id: int,
        path: str,
        content: str,
        updated_by: Optional[int] = None,
    ) -> CodeEditorFileModel:
        async with self.async_session() as session:
            result = await session.execute(
                select(CodeEditorFileModel)
                .where(
                    CodeEditorFileModel.organization_id == organization_id,
                    CodeEditorFileModel.path == path,
                )
                .with_for_update()
            )
            file = result.scalars().first()
            if file:
                file.content = content
                file.updated_by = updated_by
                file.updated_at = datetime.now(UTC)
            else:
                file = CodeEditorFileModel(
                    organization_id=organization_id,
                    path=path,
                    content=content,
                    updated_by=updated_by,
                )
                session.add(file)
            try:
                await session.commit()
            except Exception:
                await session.rollback()
                raise
            await session.refresh(file)
            return file

    async def delete_code_editor_file(self, organization_id: int, path: str) -> bool:
        async with self.async_session() as session:
            result = await session.execute(
                select(CodeEditorFileModel).where(
                    CodeEditorFileModel.organization_id == organization_id,
                    CodeEditorFileModel.path == path,
                )
            )
            file = result.scalars().first()
            if not file:
                return False
            await session.delete(file)
            try:
                await session.commit()
            except Exception:
                await session.rollback()
                raise
            return True

    # ------------------------------------------------------------------
    # Versions (immutable snapshots)
    # ------------------------------------------------------------------

    async def create_code_editor_version(
        self,
        organization_id: int,
        files: dict[str, str],
        description: Optional[str] = None,
        created_by: Optional[int] = None,
    ) -> CodeEditorVersionModel:
        """Snapshot the draft.

        The version number is allocated inside the transaction that inserts the
        row, so two people clicking Create Version at once get 4 and 5 rather
        than both getting 4 and one losing.
        """
        async with self.async_session() as session:
            # A transaction-scoped advisory lock, not SELECT ... FOR UPDATE:
            # PostgreSQL refuses row locks on an aggregate ("FOR UPDATE is not
            # allowed with aggregate functions"), and there is no row to lock
            # for the *next* number anyway. The lock is keyed on this table and
            # this organization, so two orgs never wait on each other. It is
            # released when the transaction ends, committed or not.
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext('code_editor_versions'), :org)"),
                {"org": organization_id},
            )
            result = await session.execute(
                select(
                    func.coalesce(func.max(CodeEditorVersionModel.version_number), 0)
                ).where(CodeEditorVersionModel.organization_id == organization_id)
            )
            next_number = int(result.scalar() or 0) + 1

            version = CodeEditorVersionModel(
                organization_id=organization_id,
                version_number=next_number,
                description=description,
                files=files,
                created_by=created_by,
            )
            session.add(version)
            try:
                await session.commit()
            except Exception:
                await session.rollback()
                raise
            await session.refresh(version)
            return version

    async def list_code_editor_versions(
        self, organization_id: int, limit: int = 50
    ) -> list[CodeEditorVersionModel]:
        async with self.async_session() as session:
            result = await session.execute(
                select(CodeEditorVersionModel)
                .where(CodeEditorVersionModel.organization_id == organization_id)
                .order_by(CodeEditorVersionModel.version_number.desc())
                .limit(limit)
            )
            return list(result.scalars().all())

    async def get_code_editor_version(
        self, organization_id: int, version_number: int
    ) -> Optional[CodeEditorVersionModel]:
        async with self.async_session() as session:
            result = await session.execute(
                select(CodeEditorVersionModel).where(
                    CodeEditorVersionModel.organization_id == organization_id,
                    CodeEditorVersionModel.version_number == version_number,
                )
            )
            return result.scalars().first()

    async def mark_code_editor_version_deployed(
        self, organization_id: int, version_number: int
    ) -> Optional[CodeEditorVersionModel]:
        """Stamp a version as deployed.

        Deploying an older version is a rollback and needs no extra state: the
        deployed version is simply whichever has the latest deployed_at.
        """
        async with self.async_session() as session:
            result = await session.execute(
                select(CodeEditorVersionModel)
                .where(
                    CodeEditorVersionModel.organization_id == organization_id,
                    CodeEditorVersionModel.version_number == version_number,
                )
                .with_for_update()
            )
            version = result.scalars().first()
            if not version:
                return None
            version.deployed_at = datetime.now(UTC)
            try:
                await session.commit()
            except Exception:
                await session.rollback()
                raise
            await session.refresh(version)
            return version

    async def get_deployed_code_editor_version(
        self, organization_id: int
    ) -> Optional[CodeEditorVersionModel]:
        async with self.async_session() as session:
            result = await session.execute(
                select(CodeEditorVersionModel)
                .where(
                    CodeEditorVersionModel.organization_id == organization_id,
                    CodeEditorVersionModel.deployed_at.isnot(None),
                )
                .order_by(CodeEditorVersionModel.deployed_at.desc())
                .limit(1)
            )
            return result.scalars().first()

    # ------------------------------------------------------------------
    # Environment variables
    # ------------------------------------------------------------------

    async def list_code_editor_env_vars(
        self, organization_id: int
    ) -> list[CodeEditorEnvVarModel]:
        async with self.async_session() as session:
            result = await session.execute(
                select(CodeEditorEnvVarModel)
                .where(CodeEditorEnvVarModel.organization_id == organization_id)
                .order_by(CodeEditorEnvVarModel.key)
            )
            return list(result.scalars().all())

    async def upsert_code_editor_env_var(
        self,
        organization_id: int,
        key: str,
        value_encrypted: str,
        value_hint: Optional[str] = None,
        value_length: Optional[int] = None,
    ) -> CodeEditorEnvVarModel:
        async with self.async_session() as session:
            result = await session.execute(
                select(CodeEditorEnvVarModel)
                .where(
                    CodeEditorEnvVarModel.organization_id == organization_id,
                    CodeEditorEnvVarModel.key == key,
                )
                .with_for_update()
            )
            var = result.scalars().first()
            if var:
                var.value_encrypted = value_encrypted
                var.value_hint = value_hint
                var.value_length = value_length
                var.updated_at = datetime.now(UTC)
            else:
                var = CodeEditorEnvVarModel(
                    organization_id=organization_id,
                    key=key,
                    value_encrypted=value_encrypted,
                    value_hint=value_hint,
                    value_length=value_length,
                )
                session.add(var)
            try:
                await session.commit()
            except Exception:
                await session.rollback()
                raise
            await session.refresh(var)
            return var

    async def delete_code_editor_env_var(self, organization_id: int, key: str) -> bool:
        async with self.async_session() as session:
            result = await session.execute(
                select(CodeEditorEnvVarModel).where(
                    CodeEditorEnvVarModel.organization_id == organization_id,
                    CodeEditorEnvVarModel.key == key,
                )
            )
            var = result.scalars().first()
            if not var:
                return False
            await session.delete(var)
            try:
                await session.commit()
            except Exception:
                await session.rollback()
                raise
            return True
