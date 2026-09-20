"""Tests for phone-number masking: the format, the per-organization switch, and
the places it is applied (CSV export here; the call-report route is covered in
test_call_report_db.py)."""

import csv
import io
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.db.models import OrganizationConfigurationModel, OrganizationModel
from api.services.phone_masking import (
    apply_run_masking,
    mask_phone_fields,
    mask_phone_number,
    phone_masking_enabled_from_configuration_value,
    set_phone_masking_for_organization,
    should_mask_phone_numbers,
)
from api.services.reports.run_report import build_run_report_csv


class TestMaskPhoneNumber:
    @pytest.mark.parametrize(
        "raw, masked",
        [
            ("+919876543210", "+91 98••••3210"),  # the agreed example
            ("919876543210", "+91 98••••3210"),  # country code without the plus
            ("9876543210", "98••••3210"),  # national only
            ("+91 98765 43210", "+91 98••••3210"),  # formatting is ignored
            ("+14155552671", "+1 41••••2671"),  # a one-digit country code
            ("12345", "•••45"),  # too short to keep both ends
            ("ab", "••••"),  # not a number at all
        ],
    )
    def test_format(self, raw, masked):
        assert mask_phone_number(raw) == masked

    def test_empty_values_pass_through(self):
        assert mask_phone_number(None) is None
        assert mask_phone_number("") == ""

    def test_the_middle_digits_never_survive(self):
        masked = mask_phone_number("+919876543210")

        assert "7654" not in masked


class TestMaskContexts:
    def test_only_known_phone_keys_are_masked_and_the_input_is_untouched(self):
        context = {
            "caller_number": "+919876543210",
            "called_number": "+911800000000",
            "customer_name": "Rahul",
            "count": 3,
        }

        masked = mask_phone_fields(context)

        assert masked["caller_number"] == "+91 98••••3210"
        assert masked["called_number"] == "+91 18••••0000"
        assert masked["customer_name"] == "Rahul"
        assert masked["count"] == 3
        assert context["caller_number"] == "+919876543210"

    def test_non_dict_context_passes_through(self):
        assert mask_phone_fields(None) is None

    def test_run_dict_masking(self):
        run = {
            "phone_number": "+919876543210",
            "caller_number": "+919876543210",
            "called_number": None,
            "initial_context": {"caller_number": "+919876543210"},
            "gathered_context": {"customer_phone_number": "9876543210"},
            "name": "unchanged",
        }

        apply_run_masking(run)

        assert run["phone_number"] == "+91 98••••3210"
        assert run["caller_number"] == "+91 98••••3210"
        assert run["called_number"] is None
        assert run["initial_context"]["caller_number"] == "+91 98••••3210"
        assert run["gathered_context"]["customer_phone_number"] == "98••••3210"
        assert run["name"] == "unchanged"


class TestConfigurationValue:
    def test_only_an_explicit_enabled_true_turns_it_on(self):
        assert phone_masking_enabled_from_configuration_value({"enabled": True}) is True
        assert (
            phone_masking_enabled_from_configuration_value({"enabled": False}) is False
        )
        assert phone_masking_enabled_from_configuration_value({}) is False
        assert phone_masking_enabled_from_configuration_value(None) is False
        assert phone_masking_enabled_from_configuration_value("yes") is False


class TestCsvExport:
    @staticmethod
    def _run(phone):
        return SimpleNamespace(
            id=1,
            campaign_id=None,
            workflow_id=7,
            definition_id=None,
            created_at=datetime(2026, 9, 17, tzinfo=UTC),
            initial_context={"phone_number": phone},
            gathered_context={"mapped_call_disposition": "user_hangup"},
            usage_info={"call_duration_seconds": 30},
            public_access_token=None,
        )

    @staticmethod
    def _phone_column(output: io.StringIO) -> str:
        rows = list(csv.reader(output))
        return rows[1][rows[0].index("Phone Number")]

    def test_number_is_full_by_default_and_masked_on_request(self):
        runs = [self._run("+919876543210")]

        assert self._phone_column(build_run_report_csv(runs)) == "+919876543210"
        assert (
            self._phone_column(build_run_report_csv(runs, mask_phone=True))
            == "+91 98••••3210"
        )


@pytest.fixture(scope="module")
async def db_session_factory(setup_test_database):
    from api.db import db_client

    engine = create_async_engine(setup_test_database, echo=False)
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    original_engine = db_client.engine
    original_session = db_client.async_session
    db_client.engine = engine
    db_client.async_session = session_factory

    yield session_factory

    db_client.engine = original_engine
    db_client.async_session = original_session
    await engine.dispose()


async def _create_org(db_session_factory) -> int:
    async with db_session_factory() as session:
        org = OrganizationModel(provider_id=f"test-org-{uuid.uuid4().hex[:8]}")
        session.add(org)
        await session.flush()
        await session.commit()
        return org.id


async def _delete_org(db_session_factory, organization_id: int) -> None:
    async with db_session_factory() as session:
        await session.execute(
            delete(OrganizationConfigurationModel).where(
                OrganizationConfigurationModel.organization_id == organization_id
            )
        )
        await session.execute(
            delete(OrganizationModel).where(OrganizationModel.id == organization_id)
        )
        await session.commit()


def _user(organization_id, *, superuser=False):
    return SimpleNamespace(
        id=1, is_superuser=superuser, selected_organization_id=organization_id
    )


class TestShouldMask:
    async def test_off_until_the_organization_turns_it_on(self, db_session_factory):
        org_id = await _create_org(db_session_factory)
        other_id = await _create_org(db_session_factory)
        try:
            assert await should_mask_phone_numbers(_user(org_id)) is False

            await set_phone_masking_for_organization(org_id, True)

            assert await should_mask_phone_numbers(_user(org_id)) is True
            # The switch belongs to one organization only.
            assert await should_mask_phone_numbers(_user(other_id)) is False

            await set_phone_masking_for_organization(org_id, False)
            assert await should_mask_phone_numbers(_user(org_id)) is False
        finally:
            await _delete_org(db_session_factory, org_id)
            await _delete_org(db_session_factory, other_id)

    async def test_superadmins_always_see_full_numbers(self, db_session_factory):
        org_id = await _create_org(db_session_factory)
        try:
            await set_phone_masking_for_organization(org_id, True)

            assert (
                await should_mask_phone_numbers(_user(org_id, superuser=True)) is False
            )
        finally:
            await _delete_org(db_session_factory, org_id)

    async def test_no_organization_means_no_masking(self, db_session_factory):
        assert await should_mask_phone_numbers(_user(None)) is False
