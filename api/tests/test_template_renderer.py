import pytest

from api.utils.template_renderer import render_template


def test_initial_context_prefix_resolves_against_flat_context():
    context = {
        "first_name": "Abhishek",
        "runtime_configuration": {
            "realtime_model": "gpt-realtime-2",
        },
    }

    assert (
        render_template("Hi {{initial_context.first_name | there}}", context)
        == "Hi Abhishek"
    )
    assert (
        render_template(
            "Model {{initial_context.runtime_configuration.realtime_model}}", context
        )
        == "Model gpt-realtime-2"
    )


def test_initial_context_prefix_prefers_explicit_initial_context():
    context = {
        "first_name": "Flat",
        "initial_context": {
            "first_name": "Nested",
        },
    }

    assert render_template("Hi {{initial_context.first_name}}", context) == "Hi Nested"


def test_initial_context_prefix_uses_fallback_when_missing_from_both_contexts():
    assert (
        render_template("Hi {{initial_context.first_name | there}}", {}) == "Hi there"
    )


class TestFormatFilters:
    """``digits`` / ``phone_digits`` reshape a number for APIs that reject
    ``+91...`` (a WhatsApp send wanting ``919018737669``, say)."""

    def test_digits_strips_everything_but_digits(self):
        ctx = {"caller_number": "+91 90187-37669"}
        assert render_template("{{caller_number | digits}}", ctx) == "919018737669"

    @pytest.mark.parametrize(
        "number",
        ["+919018737669", "919018737669", "9018737669", "09018737669", "+91 90187 37669"],
    )
    def test_phone_digits_always_carries_the_country_code(self, number):
        ctx = {"caller_number": number}
        assert render_template("{{caller_number | phone_digits}}", ctx) == "919018737669"

    def test_phone_digits_leaves_other_countries_alone(self):
        ctx = {"caller_number": "+14155550123"}
        assert render_template("{{caller_number | phone_digits}}", ctx) == "14155550123"

    def test_works_through_initial_context_paths(self):
        ctx = {"caller_number": "+919018737669"}
        assert (
            render_template("{{initial_context.caller_number | digits}}", ctx)
            == "919018737669"
        )

    def test_a_missing_number_stays_empty(self):
        """So a required preset still fails loudly instead of sending the
        word "digits"."""
        assert render_template("{{caller_number | phone_digits}}", {}) == ""

    def test_other_filter_names_are_still_defaults(self):
        assert render_template("{{name | there}}", {}) == "there"
        assert render_template("{{name | fallback:Guest}}", {}) == "Guest"
