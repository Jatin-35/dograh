"""In-app lead forms are emailed to the team and always logged."""

import pytest

from api.services import leads


def test_format_lead_orders_known_fields_and_keeps_unknown_ones():
    subject, body = leads.format_lead(
        "hire_expert",
        {
            "phone": "+919876543210",
            "name": "Asha",
            "company": "Acme",
            "volume": "5k-100k",
            "extra": "kept",
            "jobTitle": "  ",
        },
        "asha@acme.in",
        7,
    )
    assert subject == "BotrixAI Voicebot Lead – Build My AI Agent: Acme"
    lines = body.splitlines()
    assert lines[:4] == [
        "Name: Asha",
        "Company: Acme",
        "Phone: +919876543210",
        "Monthly call volume: 5k-100k",
    ]
    assert "extra: kept" in lines
    assert not any(line.startswith("Job title") for line in lines)
    assert "Signed-in account: asha@acme.in" in lines
    assert "Organization id: 7" in lines


def test_enterprise_subject_falls_back_to_work_email():
    subject, body = leads.format_lead(
        "enterprise", {"workEmail": "it@corp.in"}, None, None
    )
    assert subject == "BotrixAI Voicebot Lead – Enterprise: it@corp.in"
    assert "Work email: it@corp.in" in body
    assert "Signed-in account: unknown" in body


def test_recipients_default_and_override(monkeypatch):
    monkeypatch.delenv("LEADS_EMAIL", raising=False)
    assert leads.leads_recipients() == ["contact@botrixai.com"]
    monkeypatch.setenv("LEADS_EMAIL", "a@x.com, b@x.com ,")
    assert leads.leads_recipients() == ["a@x.com", "b@x.com"]


@pytest.mark.asyncio
@pytest.mark.parametrize("sent", [True, False])
async def test_deliver_lead_emails_the_team(monkeypatch, sent):
    calls = []

    async def fake_send(recipients, subject, body):
        calls.append((list(recipients), subject, body))
        return sent

    monkeypatch.delenv("LEADS_EMAIL", raising=False)
    monkeypatch.setattr(leads, "send_email", fake_send)
    result = await leads.deliver_lead("hire_expert", {"name": "Asha"}, "a@b.in", 1)
    assert result is sent
    assert calls[0][0] == ["contact@botrixai.com"]
    assert calls[0][1] == "BotrixAI Voicebot Lead – Build My AI Agent: Asha"
