"""
Tests for ai.py's resilience behavior: the Claude client is always mocked
here (no real API calls, no network, no cost) so these run instantly and
deterministically. The point isn't testing Claude's output quality -- that's
inherently non-deterministic -- it's proving the bot never crashes or goes
silent when the API call fails, which is the exact bug this project hit in
production.
"""

import json

import pytest

import ai


class _FakeContentBlock:
    def __init__(self, text):
        self.text = text


class _FakeResponse:
    def __init__(self, text):
        self.content = [_FakeContentBlock(text)]


class _FakeClient:
    """Stands in for anthropic.Anthropic. `create_return` can be a string
    (returned as the response text) or an Exception instance/class (raised
    instead), so tests can simulate both success and failure paths."""

    def __init__(self, create_return):
        self._create_return = create_return
        self.messages = self

    def create(self, **kwargs):
        if isinstance(self._create_return, Exception):
            raise self._create_return
        return _FakeResponse(self._create_return)


@pytest.fixture(autouse=True)
def _reset_client_singleton(monkeypatch):
    # ai.py caches the client in a module-level global; make sure each test
    # starts from a clean slate regardless of test order.
    monkeypatch.setattr(ai, "_client", None)
    yield


def _mock_client(monkeypatch, create_return):
    fake = _FakeClient(create_return)
    monkeypatch.setattr(ai, "_get_client", lambda: fake)
    return fake


# ---------- parse_message: happy path ----------

def test_parse_message_returns_parsed_json(monkeypatch):
    payload = {
        "is_expense": True, "amount": 12.5, "currency": None, "description": "lunch",
        "category": "Food", "is_claimable": False, "needs_clarification": False,
        "clarification_question": None, "casual_reply": None,
    }
    _mock_client(monkeypatch, json.dumps(payload))
    result = ai.parse_message("spent 12.50 on lunch")
    assert result["is_expense"] is True
    assert result["amount"] == 12.5


def test_parse_message_strips_markdown_code_fence(monkeypatch):
    payload = {"is_expense": True, "amount": 5, "currency": None, "description": "coffee",
               "category": "Food", "is_claimable": False, "needs_clarification": False,
               "clarification_question": None, "casual_reply": None}
    _mock_client(monkeypatch, f"```json\n{json.dumps(payload)}\n```")
    result = ai.parse_message("coffee 5")
    assert result["amount"] == 5


def test_parse_message_returns_casual_reply_for_non_expense(monkeypatch):
    payload = {"is_expense": False, "amount": None, "currency": None, "description": None,
               "category": None, "is_claimable": None, "needs_clarification": False,
               "clarification_question": None, "casual_reply": "Hey! Doing well -- anything to log?"}
    _mock_client(monkeypatch, json.dumps(payload))
    result = ai.parse_message("hey how's it going")
    assert result["is_expense"] is False
    assert result["casual_reply"] == "Hey! Doing well -- anything to log?"


# ---------- parse_message: resilience (the actual production bug) ----------

def test_parse_message_never_raises_on_api_failure(monkeypatch):
    """This is the regression test for the real bug: an unhandled exception
    from the Claude call (auth failure, network blip, dependency mismatch --
    exactly what happened with the anthropic/httpx version conflict) must
    never propagate out of parse_message. It must come back as a normal,
    gracefully-worded clarification dict instead."""
    _mock_client(monkeypatch, TypeError("Client.__init__() got an unexpected keyword argument 'proxies'"))
    result = ai.parse_message("test message")
    assert result["is_expense"] is False
    assert result["needs_clarification"] is True
    assert result["clarification_question"]  # some non-empty message, not None
    assert result["casual_reply"] is None


def test_parse_message_falls_back_on_invalid_json(monkeypatch):
    _mock_client(monkeypatch, "this is not json at all")
    result = ai.parse_message("garbled")
    assert result["is_expense"] is False
    assert result["needs_clarification"] is True


# ---------- categorize: resilience ----------

def test_categorize_returns_model_choice_when_valid(monkeypatch):
    _mock_client(monkeypatch, "Transport")
    assert ai.categorize("uber ride") == "Transport"


def test_categorize_defaults_to_other_on_invalid_category(monkeypatch):
    _mock_client(monkeypatch, "NotARealCategory")
    assert ai.categorize("mystery purchase") == "Other"


def test_categorize_never_raises_on_api_failure(monkeypatch):
    _mock_client(monkeypatch, ConnectionError("network blip"))
    assert ai.categorize("anything") == "Other"
