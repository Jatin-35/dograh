"""Webhook Sync building blocks: phone normalization, field mapping, payload
parsing, authentication, log redaction and settings validation."""

import json

import pytest
from pydantic import ValidationError

from api.schemas.webhook_sync import CallSettings, CallingHours, FieldMapping
from api.services.webhook_sync.auth import (
    auth_failure,
    generate_secret,
    is_authorized,
    sign,
)
from api.services.webhook_sync.mapping import get_path, map_lead, variable_name
from api.services.webhook_sync.payload import (
    MAX_BODY_BYTES,
    MAX_LEADS_PER_REQUEST,
    PayloadError,
    parse_leads,
)
from api.services.webhook_sync.phone import normalize_indian_mobile
from api.services.webhook_sync.request_log import (
    REDACTED,
    client_ip,
    install_access_log_redaction,
    loggable_body,
    redact_headers,
    redact_query_token,
)

# ---------------------------------------------------------------------------
# Phone normalization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("9876543210", "+919876543210"),
        ("+91 98765 43210", "+919876543210"),
        ("+91-98765-43210", "+919876543210"),
        ("919876543210", "+919876543210"),
        ("09876543210", "+919876543210"),
        ("0091 9876543210", "+919876543210"),
        ("(+91) 98765.43210", "+919876543210"),
        (9876543210, "+919876543210"),
        (9876543210.0, "+919876543210"),
        ("6000000000", "+916000000000"),
        # Not Indian mobiles
        ("5876543210", None),  # mobiles start 6-9
        ("987654321", None),  # 9 digits
        ("98765432101", None),  # 11 digits, no 0 prefix
        ("+1 415 555 0100", None),
        ("0112345678", None),  # landline
        ("", None),
        (None, None),
        (True, None),
        (98765.4321, None),
        ("abc", None),
    ],
)
def test_normalize_indian_mobile(raw, expected):
    assert normalize_indian_mobile(raw) == expected


# ---------------------------------------------------------------------------
# Field mapping
# ---------------------------------------------------------------------------

NESTED = {
    "data": {
        "lead": {"mobile": "98765 43210", "name": "Rahul Kumar", "city": "Patna"},
        "items": [{"sku": "TANK-1000"}, {"sku": "TANK-500"}],
    },
    "Lead Source": "Facebook Ads",
}


@pytest.mark.parametrize(
    "path,expected",
    [
        ("data.lead.mobile", "98765 43210"),
        ("data.items[1].sku", "TANK-500"),
        ("data.items.sku", "TANK-1000"),  # bare name on a list = first element
        ("DATA.Lead.MOBILE", "98765 43210"),  # case-insensitive fallback
        ("data.missing", None),
        ("data.items[9].sku", None),
        ("", None),
    ],
)
def test_get_path(path, expected):
    assert get_path(NESTED, path) == expected


def test_explicit_mapping_reads_nested_fields():
    lead = map_lead(
        NESTED,
        {
            "phone": "data.lead.mobile",
            "name": "data.lead.name",
            "custom": {"product": "data.items[0].sku"},
        },
    )
    assert lead.phone_raw == "98765 43210"
    assert lead.name == "Rahul Kumar"
    assert lead.variables["product"] == "TANK-1000"
    assert lead.variables["name"] == "Rahul Kumar"


def test_common_field_names_are_found_without_any_mapping():
    lead = map_lead(
        {
            "Mobile Number": "9876543210",
            "Full Name": "Asha",
            "Email ID": "a@x.in",
            "Lead Source": "Website",
            "City": "Noida",
            "Prospect Id": "LSQ-1",
        }
    )
    assert lead.phone_raw == "9876543210"
    assert lead.name == "Asha"
    assert lead.email == "a@x.in"
    assert lead.source == "Website"
    assert lead.city == "Noida"
    assert lead.external_lead_id == "LSQ-1"


def test_fields_inside_common_wrappers_are_found():
    lead = map_lead({"lead": {"phone": "9876543210"}, "event": "lead.created"})
    assert lead.phone_raw == "9876543210"
    lead = map_lead({"data": {"lead": {"phone": "9876543210", "name": "Asha"}}})
    assert (lead.phone_raw, lead.name) == ("9876543210", "Asha")
    # Three wrappers deep is too far to guess; that needs an explicit mapping.
    assert (
        map_lead({"a": {"data": {"lead": {"x": {"phone": "9876543210"}}}}}).phone_raw
        is None
    )


def test_top_level_fields_win_over_nested_ones():
    lead = map_lead({"phone": "9876543210", "data": {"phone": "9123456780"}})
    assert lead.phone_raw == "9876543210"


def test_first_and_last_name_are_combined():
    assert map_lead({"FirstName": "Rahul", "LastName": "Kumar"}).name == "Rahul Kumar"
    assert map_lead({"first_name": "Rahul"}).name == "Rahul"


def test_every_plain_field_becomes_a_call_variable():
    lead = map_lead(
        {
            "phone": "9876543210",
            "Budget (Rs)": 10000,
            "Interested": True,
            "tags": ["a", "b"],
            "meta": {"x": 1},
        }
    )
    assert lead.variables["budget_rs"] == "10000"
    assert lead.variables["interested"] == "true"
    # Objects and lists are only taken when mapped explicitly.
    assert "tags" not in lead.variables and "meta" not in lead.variables


def test_explicit_mapping_wins_over_detection():
    lead = map_lead(
        {"phone": "1111111111", "alt": {"mobile": "9876543210"}},
        {"phone": "alt.mobile"},
    )
    assert lead.phone_raw == "9876543210"


def test_long_values_are_clipped():
    lead = map_lead({"phone": "9876543210", "note": "x" * 5000})
    assert len(lead.variables["note"]) == 500


def test_variables_are_capped_keeping_mapped_and_standard_fields():
    payload = {"phone": "9876543210", "name": "Asha", "deep": {"sku": "TANK"}}
    payload.update({f"field_{i}": str(i) for i in range(80)})
    lead = map_lead(payload, {"custom": {"product": "deep.sku"}})
    assert len(lead.variables) == 50
    assert lead.variables["product"] == "TANK"
    assert lead.variables["name"] == "Asha"


def test_variable_names_are_prompt_safe():
    assert variable_name("Lead Source") == "lead_source"
    assert variable_name("  Budget (Rs)  ") == "budget_rs"
    assert variable_name("---") == ""


# ---------------------------------------------------------------------------
# Payload parsing
# ---------------------------------------------------------------------------


def test_json_object_is_one_lead_and_array_is_many():
    assert parse_leads(b'{"phone": "1"}', "application/json") == [{"phone": "1"}]
    assert parse_leads(b'[{"a": 1}, {"a": 2}]', "application/json; charset=utf-8") == [
        {"a": 1},
        {"a": 2},
    ]


def test_form_posts_are_parsed():
    assert parse_leads(
        b"name=Rahul+Kumar&mobile=9876543210", "application/x-www-form-urlencoded"
    ) == [{"name": "Rahul Kumar", "mobile": "9876543210"}]
    # Repeated keys keep every value.
    assert parse_leads(b"tag=a&tag=b", "application/x-www-form-urlencoded") == [
        {"tag": ["a", "b"]}
    ]
    # Some CRMs post form data without a content type.
    assert parse_leads(b"mobile=9876543210", None) == [{"mobile": "9876543210"}]


def test_a_utf8_bom_is_ignored():
    assert parse_leads(
        b"\xef\xbb\xbf" + json.dumps({"a": 1}).encode(), "application/json"
    ) == [{"a": 1}]


@pytest.mark.parametrize(
    "body,content_type,status",
    [
        (b"", "application/json", 400),
        (b"   ", "application/json", 400),
        (b"{not json", "application/json", 400),
        (b"[]", "application/json", 400),
        (b'"just a string"', "application/json", 400),
        (b"[1, 2]", "application/json", 400),
        (b"\xff\xfe", "application/json", 400),
        (b"x" * (MAX_BODY_BYTES + 1), "application/json", 413),
        (
            json.dumps([{"a": i} for i in range(MAX_LEADS_PER_REQUEST + 1)]).encode(),
            "application/json",
            413,
        ),
    ],
    ids=[
        "empty",
        "blank",
        "broken-json",
        "empty-list",
        "string",
        "list-of-numbers",
        "not-utf8",
        "too-large",
        "too-many-leads",
    ],
)
def test_bad_bodies_are_rejected(body, content_type, status):
    with pytest.raises(PayloadError) as exc:
        parse_leads(body, content_type)
    assert exc.value.status_code == status


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

SECRET = "s3cr3t-value"
BODY = b'{"phone": "9876543210"}'


def test_generated_secrets_are_long_and_unique():
    secrets = {generate_secret() for _ in range(50)}
    assert len(secrets) == 50
    assert all(len(s) >= 40 for s in secrets)


def test_api_key_mode():
    assert is_authorized("api_key", SECRET, {"X-API-Key": SECRET}, {}, BODY)
    assert is_authorized("api_key", SECRET, {"x-api-key": f" {SECRET} "}, {}, BODY)
    assert not is_authorized("api_key", SECRET, {"X-API-Key": "wrong"}, {}, BODY)
    assert not is_authorized("api_key", SECRET, {}, {}, BODY)
    # The secret in the URL does not satisfy api_key mode.
    assert not is_authorized("api_key", SECRET, {}, {"token": SECRET}, BODY)


NOW = 1_800_000_000
TS = str(NOW)


def test_hmac_mode():
    signature = sign(SECRET, BODY, TS)
    headers = {"X-Botrix-Signature": signature, "X-Botrix-Timestamp": TS}
    assert is_authorized("hmac", SECRET, headers, {}, BODY, now=NOW)
    assert is_authorized(
        "hmac",
        SECRET,
        {**headers, "X-Botrix-Signature": f"sha256={signature}"},
        {},
        BODY,
        now=NOW,
    )
    assert is_authorized(
        "hmac",
        SECRET,
        {**headers, "X-Botrix-Signature": signature.upper()},
        {},
        BODY,
        now=NOW,
    )
    # A changed body, another secret, or no signature at all fails.
    assert not is_authorized("hmac", SECRET, headers, {}, BODY + b" ", now=NOW)
    assert not is_authorized(
        "hmac",
        SECRET,
        {**headers, "X-Botrix-Signature": sign("other", BODY, TS)},
        {},
        BODY,
        now=NOW,
    )
    assert not is_authorized("hmac", SECRET, {"X-API-Key": SECRET}, {}, BODY, now=NOW)


def test_hmac_signature_covers_the_timestamp():
    # Moving the timestamp forward (to dodge the age check) breaks the signature.
    signature = sign(SECRET, BODY, TS)
    later = str(NOW + 600)
    headers = {"X-Botrix-Signature": signature, "X-Botrix-Timestamp": later}
    assert (
        auth_failure("hmac", SECRET, headers, {}, BODY, now=NOW + 600)
        == "Wrong signature"
    )


@pytest.mark.parametrize(
    "offset,ok",
    [(0, True), (299, True), (-299, True), (301, False), (-301, False), (86400, False)],
)
def test_hmac_rejects_old_or_future_timestamps(offset, ok):
    # A request signed at NOW, checked `offset` seconds later.
    headers = {"X-Botrix-Signature": sign(SECRET, BODY, TS), "X-Botrix-Timestamp": TS}
    assert is_authorized("hmac", SECRET, headers, {}, BODY, now=NOW + offset) is ok


def test_hmac_failure_reasons():
    signature = sign(SECRET, BODY, TS)
    assert "Missing" in auth_failure(
        "hmac", SECRET, {"X-Botrix-Signature": signature}, {}, BODY, now=NOW
    )
    assert "unix seconds" in auth_failure(
        "hmac",
        SECRET,
        {"X-Botrix-Signature": signature, "X-Botrix-Timestamp": "yesterday"},
        {},
        BODY,
        now=NOW,
    )
    assert "5 minutes" in auth_failure(
        "hmac",
        SECRET,
        {"X-Botrix-Signature": signature, "X-Botrix-Timestamp": TS},
        {},
        BODY,
        now=NOW + 3600,
    )


def test_failure_reasons_never_contain_the_secret():
    for auth_type, headers, query in (
        ("api_key", {"X-API-Key": "wrong"}, {}),
        ("url_token", {}, {"token": "wrong"}),
        ("hmac", {"X-Botrix-Signature": "ab", "X-Botrix-Timestamp": TS}, {}),
    ):
        reason = auth_failure(auth_type, SECRET, headers, query, BODY, now=NOW)
        assert reason and SECRET not in reason


def test_url_token_mode():
    assert is_authorized("url_token", SECRET, {}, {"token": SECRET}, BODY)
    assert not is_authorized("url_token", SECRET, {}, {"token": "wrong"}, BODY)
    assert not is_authorized("url_token", SECRET, {"X-API-Key": SECRET}, {}, BODY)


def test_unknown_auth_mode_never_passes():
    assert not is_authorized(
        "none", SECRET, {"X-API-Key": SECRET}, {"token": SECRET}, BODY
    )


# ---------------------------------------------------------------------------
# Request log redaction
# ---------------------------------------------------------------------------


def test_credentials_are_redacted_from_logged_headers():
    kept = redact_headers(
        {
            "X-API-Key": SECRET,
            "Authorization": "Bearer abc",
            "X-Botrix-Signature": "sig",
            "Cookie": "a=b",
            "X-Auth-Token": "t",
            "Content-Type": "application/json",
            "User-Agent": "LeadSquared",
            "Host": "api",
            "Content-Length": "10",
        }
    )
    for name in (
        "x-api-key",
        "authorization",
        "x-botrix-signature",
        "cookie",
        "x-auth-token",
    ):
        assert kept[name] == REDACTED
    assert kept["content-type"] == "application/json"
    assert kept["user-agent"] == "LeadSquared"
    assert "host" not in kept and "content-length" not in kept
    assert SECRET not in json.dumps(kept)


def test_logged_body_is_truncated():
    text, truncated = loggable_body(b"a" * 70_000)
    assert truncated and len(text) == 64_000
    assert loggable_body(b"") == (None, False)


@pytest.mark.parametrize(
    "path,expected",
    [
        (
            "/api/v1/webhooks/inbound/u?token=SECRET",
            "/api/v1/webhooks/inbound/u?token=[redacted]",
        ),
        (
            "/api/v1/webhooks/inbound/u?a=1&token=SECRET&b=2",
            "/api/v1/webhooks/inbound/u?a=1&token=[redacted]&b=2",
        ),
        (
            "/api/v1/webhooks/inbound/u?TOKEN=SECRET",
            "/api/v1/webhooks/inbound/u?TOKEN=[redacted]",
        ),
        ("/api/v1/webhooks/inbound/u", "/api/v1/webhooks/inbound/u"),
    ],
)
def test_url_token_is_redacted(path, expected):
    assert redact_query_token(path) == expected


def test_the_server_access_log_never_shows_the_url_token():
    import logging

    install_access_log_redaction()
    install_access_log_redaction()  # idempotent
    access = logging.getLogger("uvicorn.access")
    assert (
        sum(1 for f in access.filters if type(f).__name__ == "_AccessLogTokenFilter")
        == 1
    )

    def rendered(path):
        record = logging.LogRecord(
            "uvicorn.access",
            logging.INFO,
            __file__,
            1,
            '%s - "%s %s HTTP/%s" %d',
            ("1.2.3.4:5", "POST", path, "1.1", 200),
            None,
        )
        for f in access.filters:
            f.filter(record)
        return record.getMessage()

    line = rendered("/api/v1/webhooks/inbound/u?token=TOP-SECRET")
    assert "TOP-SECRET" not in line and "token=[redacted]" in line
    # Other routes are left alone.
    assert "keep=me" in rendered("/api/v1/other?keep=me")


def test_client_ip_behind_proxies():
    assert client_ip({"X-Forwarded-For": "1.2.3.4, 10.0.0.1"}, "10.0.0.9") == "1.2.3.4"
    assert client_ip({"CF-Connecting-IP": "5.6.7.8"}, "10.0.0.9") == "5.6.7.8"
    assert client_ip({}, "10.0.0.9") == "10.0.0.9"
    assert client_ip({}, None) is None


# ---------------------------------------------------------------------------
# Settings validation
# ---------------------------------------------------------------------------


def test_default_call_settings_are_trai_friendly():
    settings = CallSettings()
    assert (settings.calling_hours.start, settings.calling_hours.end) == (
        "09:00",
        "21:00",
    )
    assert settings.calling_hours.timezone == "Asia/Kolkata"
    assert settings.dedupe_window_hours == 24
    assert settings.delay_minutes == 0


@pytest.mark.parametrize(
    "hours",
    [
        {"start": "9:00"},
        {"end": "24:00"},
        {"start": "21:00", "end": "09:00"},
        {"timezone": "Mars/Base"},
        {"days": []},
        {"days": [7]},
    ],
)
def test_invalid_calling_hours_are_rejected(hours):
    with pytest.raises(ValidationError):
        CallingHours(**hours)


def test_callback_url_must_be_https():
    assert CallSettings(callback_url="https://crm.example/hook").callback_url
    with pytest.raises(ValidationError):
        CallSettings(callback_url="http://crm.example/hook")


def test_field_mapping_rejects_unknown_fields_and_too_many_custom():
    with pytest.raises(ValidationError):
        FieldMapping(mobile="x")
    with pytest.raises(ValidationError):
        FieldMapping(custom={f"f{i}": "p" for i in range(51)})


# ---------------------------------------------------------------------------
# Mapping preview (the dashboard's mapping screen)
# ---------------------------------------------------------------------------


def test_preview_maps_a_nested_leadsquared_style_sample():
    from api.services.webhook_sync.preview import preview_mapping

    sample = json.dumps(
        {
            "Current": {
                "ProspectID": "abc-1",
                "FirstName": "Rahul",
                "LastName": "Kumar",
                "Phone": "+91-9876543210",
                "mx_City": "Patna",
            }
        }
    )
    result = preview_mapping(
        sample, "application/json", {"custom": {"city": "Current.mx_City"}}
    )
    assert result["outcome"] == "received"
    assert result["phone"] == "+919876543210"
    assert result["fields"]["name"] == "Rahul Kumar"
    assert result["fields"]["external_lead_id"] == "abc-1"
    assert result["variables"]["city"] == "Patna"
    assert {"path": "Current.Phone", "value": "+91-9876543210"} in result["paths"]


def test_preview_explains_a_missing_or_invalid_phone():
    from api.services.webhook_sync.preview import preview_mapping

    missing = preview_mapping('{"name": "A"}', "application/json", {})
    assert missing["outcome"] == "rejected" and "phone" in missing["reason"]
    invalid = preview_mapping('{"mobile": "12345"}', "application/json", {})
    assert invalid["outcome"] == "invalid_number" and invalid["phone"] is None


def test_preview_accepts_form_data_and_arrays_and_rejects_garbage():
    from api.services.webhook_sync.preview import preview_mapping

    form = preview_mapping(
        "mobile=9876543210&name=Asha", "application/x-www-form-urlencoded", {}
    )
    assert form["phone"] == "+919876543210" and form["fields"]["name"] == "Asha"
    bulk = preview_mapping(
        '[{"mobile": "9876543210"}, {"mobile": "9123456780"}]', None, {}
    )
    assert bulk["lead_count"] == 2
    with pytest.raises(PayloadError):
        preview_mapping("{not json", "application/json", {})


def test_leaf_paths_cover_nesting_lists_and_are_capped():
    from api.services.webhook_sync.preview import MAX_PATHS, leaf_paths

    paths = leaf_paths({"a": {"b": 1}, "leads": [{"phone": "9"}], "n": None})
    assert paths == [
        {"path": "a.b", "value": "1"},
        {"path": "leads[0].phone", "value": "9"},
    ]
    assert len(leaf_paths({f"k{i}": i for i in range(1000)})) == MAX_PATHS


def test_leadsquared_mx_prefixed_fields_are_detected():
    lead = map_lead(
        {
            "Current": {
                "ProspectID": "P-1",
                "FirstName": "Rahul",
                "Phone": "9876543210",
                "mx_City": "Patna",
                "mx_Language": "Hindi",
            }
        }
    )
    assert (lead.city, lead.language_preference) == ("Patna", "Hindi")
    assert lead.variables["city"] == "Patna"
    # The CRM's own field name stays available too.
    assert lead.variables["mx_city"] == "Patna"


def test_an_exact_field_beats_an_mx_prefixed_one():
    lead = map_lead({"mobile": "9876543210", "city": "Delhi", "mx_City": "Patna"})
    assert lead.city == "Delhi"


def test_leadsquared_custom_fields_are_also_variables_without_the_prefix():
    lead = map_lead(
        {"Phone": "9876543210", "mx_Budget": "50L", "mx_Plan": "Gold", "Plan": "Silver"}
    )
    assert lead.variables["budget"] == "50L"
    assert lead.variables["mx_budget"] == "50L"
    # A field the CRM sent under the plain name wins.
    assert lead.variables["plan"] == "Silver"


# A real LeadSquared lead-creation webhook (6 Oct 2026), trimmed of nothing:
# about 70 fields, of which the agent needs a handful.
_LEADSQUARED_LEAD = {
    "ProspectID": "df783132-b696-416d-9269-dbe0d7d43a2e",
    "ProspectAutoId": "1033845",
    "FirstName": "sunil webhook test",
    "LastName": None,
    "EmailAddress": None,
    "Origin": "API",
    "Phone": "+91-9876543210",
    "Mobile": None,
    "Source": "Contact Us",
    "ProspectStage": "Raw",
    "Score": "0",
    "EngagementScore": "0",
    "OwnerId": "80381a1d-dfd1-11ee-8d08-0261eba56ddf",
    "CreatedOn": "2026-10-06 17:45:19",
    "mx_Query_type": "Household",
    "NotableEvent": "Created",
    "mx_Alternate_Email_Address": "tree@qwerty.com",
    "mx_Enquired_City": "Raipur",
    "mx_Enquired_State": "Chattisgarh",
    "OwnerIdEmailAddress": "Care@vectus.in",
    "Account_CompanyName": "Vectus Polymers Private Limited",
    "Account_Address": "Vectus Polymers Private Limited{mxnewline}A101 Sector 83",
    "Org_ShortCode": "73348",
    "CanUpdate": "true",
}

_VECTUS_MAPPING = {
    "phone": "Phone",
    "name": "FirstName",
    "city": "mx_Enquired_City",
    "source": "Source",
    "custom": {"state": "mx_Enquired_State", "query_type": "mx_Query_type"},
}


def test_by_default_every_crm_field_becomes_a_variable():
    lead = map_lead(_LEADSQUARED_LEAD, _VECTUS_MAPPING)
    assert lead.variables["state"] == "Chattisgarh"
    # The clutter comes through too.
    assert lead.variables["owneridemailaddress"] == "Care@vectus.in"
    assert "account_companyname" in lead.variables


def test_only_mapped_keeps_just_the_mapped_fields():
    lead = map_lead(_LEADSQUARED_LEAD, {**_VECTUS_MAPPING, "only_mapped": True})
    assert lead.variables == {
        "state": "Chattisgarh",
        "query_type": "Household",
        "name": "sunil webhook test",
        "source": "Contact Us",
        "city": "Raipur",
    }
    # The lead itself is still read in full: the phone still reaches the call.
    assert lead.phone_raw == "+91-9876543210"
    assert normalize_indian_mobile(lead.phone_raw) == "+919876543210"


def test_only_mapped_with_nothing_mapped_keeps_the_detected_basics():
    lead = map_lead(_LEADSQUARED_LEAD, {"only_mapped": True})
    assert set(lead.variables) <= {"name", "email", "source", "city", "language_preference"}
    assert lead.variables["name"] == "sunil webhook test"
    assert lead.phone_raw == "+91-9876543210"


def test_the_switch_is_saved_with_the_mapping_and_off_by_default():
    assert FieldMapping().only_mapped is False
    saved = FieldMapping.model_validate({**_VECTUS_MAPPING, "only_mapped": True})
    assert saved.model_dump(exclude_none=True)["only_mapped"] is True
