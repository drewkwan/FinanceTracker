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


# ---------- parse_message: happy path, log_expense ----------

def test_parse_message_returns_parsed_log_expense(monkeypatch):
    payload = {
        "intent": "log_expense", "amount": 12.5, "currency": None, "description": "lunch",
        "category": "Food", "is_claimable": False,
        "target_expense_id": None, "correction_action": None, "days_ago": None,
        "new_currency": None, "new_amount": None, "new_description": None, "new_category": None,
        "clarification_question": None, "casual_reply": None,
    }
    _mock_client(monkeypatch, json.dumps(payload))
    result = ai.parse_message("spent 12.50 on lunch", [])
    assert result["intent"] == "log_expense"
    assert result["amount"] == 12.5


def test_parse_message_strips_markdown_code_fence(monkeypatch):
    payload = {"intent": "log_expense", "amount": 5, "currency": None, "description": "coffee",
               "category": "Food", "is_claimable": False,
               "target_expense_id": None, "correction_action": None, "days_ago": None,
               "new_currency": None, "new_amount": None, "new_description": None, "new_category": None,
               "clarification_question": None, "casual_reply": None}
    _mock_client(monkeypatch, f"```json\n{json.dumps(payload)}\n```")
    result = ai.parse_message("coffee 5", [])
    assert result["amount"] == 5


def test_parse_message_passes_recent_expenses_into_the_prompt(monkeypatch):
    """Regression guard for the actual reported bug: without seeing recent
    expenses, the model has no way to know which entry a correction like
    "that was for yesterday" refers to. Just checks the context is actually
    sent to the API, not what the model does with it (that's not testable
    without a real call)."""
    captured = {}

    class _CapturingClient(_FakeClient):
        def create(self, **kwargs):
            captured["messages"] = kwargs.get("messages")
            return super().create(**kwargs)

    fake = _CapturingClient(json.dumps({
        "intent": "casual", "amount": None, "currency": None, "description": None, "category": None,
        "is_claimable": None, "target_expense_id": None, "correction_action": None, "days_ago": None,
        "new_currency": None, "new_amount": None, "new_description": None, "new_category": None,
        "clarification_question": None, "casual_reply": "hey!",
    }))
    monkeypatch.setattr(ai, "_get_client", lambda: fake)
    recent = [{"id": 42, "amount": 100, "currency": "SGD", "description": "parking cashcard top-up",
               "category": "Transport", "expense_date": "2026-07-27", "is_claimable": False}]
    ai.parse_message("hi", recent)
    sent_content = captured["messages"][0]["content"]
    assert "42" in sent_content
    assert "parking cashcard top-up" in sent_content


# ---------- parse_message: casual intent ----------

def test_parse_message_returns_casual_reply_for_non_expense(monkeypatch):
    payload = {"intent": "casual", "amount": None, "currency": None, "description": None,
               "category": None, "is_claimable": None,
               "target_expense_id": None, "correction_action": None, "days_ago": None,
               "new_currency": None, "new_amount": None, "new_description": None, "new_category": None,
               "clarification_question": None, "casual_reply": "Hey! Doing well -- anything to log?"}
    _mock_client(monkeypatch, json.dumps(payload))
    result = ai.parse_message("hey how's it going", [])
    assert result["intent"] == "casual"
    assert result["casual_reply"] == "Hey! Doing well -- anything to log?"


# ---------- parse_message: correction intent ----------

def test_parse_message_returns_correction_targeting_a_recent_id(monkeypatch):
    payload = {"intent": "correction", "amount": None, "currency": None, "description": None,
               "category": None, "is_claimable": None,
               "target_expense_id": 42, "correction_action": "edit_date", "days_ago": 2,
               "new_currency": None, "new_amount": None, "new_description": None, "new_category": None,
               "clarification_question": None, "casual_reply": None}
    _mock_client(monkeypatch, json.dumps(payload))
    recent = [{"id": 42, "amount": 100, "currency": "SGD", "description": "parking cashcard top-up",
               "category": "Transport", "expense_date": "2026-07-27", "is_claimable": False}]
    result = ai.parse_message("sorry that was from two days ago", recent)
    assert result["intent"] == "correction"
    assert result["target_expense_id"] == 42
    assert result["correction_action"] == "edit_date"
    assert result["days_ago"] == 2


# ---------- parse_message: resilience (the actual production bug) ----------

def test_parse_message_never_raises_on_api_failure(monkeypatch):
    """This is the regression test for the real bug: an unhandled exception
    from the Claude call (auth failure, network blip, dependency mismatch --
    exactly what happened with the anthropic/httpx version conflict) must
    never propagate out of parse_message. It must come back as a normal,
    gracefully-worded clarification dict instead."""
    _mock_client(monkeypatch, TypeError("Client.__init__() got an unexpected keyword argument 'proxies'"))
    result = ai.parse_message("test message", [])
    assert result["intent"] == "clarification"
    assert result["clarification_question"]  # some non-empty message, not None
    assert result["casual_reply"] is None


def test_parse_message_falls_back_on_invalid_json(monkeypatch):
    _mock_client(monkeypatch, "this is not json at all")
    result = ai.parse_message("garbled", [])
    assert result["intent"] == "clarification"


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
