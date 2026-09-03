"""
Tests for the vitals addition: db.py CRUD, ai.py's extraction resilience,
and bot.py's natural-language/command logging and cross-domain correction,
including undo. Same discipline as test_meals_workouts.py: no real network,
no real Claude calls (ai._get_client is always mocked), throwaway SQLite
per test.
"""

import asyncio
import datetime as dt

import ai
import bot
import db
from conftest import CHAT
from test_ai import _mock_client  # reuse the existing fake client
from test_meals_workouts import FakeContext, FakeUpdate  # reuse the fake Telegram objects


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------- db.py: vitals ----------

def test_add_and_get_vitals_roundtrip():
    vitals_id = db.add_vitals(CHAT, weight_kg=76.6, sleep_hours=5.5, knee_pain=2, notes="felt tight")
    row = db.get_vitals(CHAT, vitals_id)
    assert row["weight_kg"] == 76.6
    assert row["sleep_hours"] == 5.5
    assert row["knee_pain"] == 2
    assert row["notes"] == "felt tight"
    assert row["vitals_date"] == db.today_str()


def test_add_vitals_only_sets_mentioned_fields():
    vitals_id = db.add_vitals(CHAT, weight_kg=76.6)
    row = db.get_vitals(CHAT, vitals_id)
    assert row["weight_kg"] == 76.6
    assert row["sleep_hours"] is None
    assert row["knee_pain"] is None


def test_edit_vitals_date_clamps_future_to_today():
    vitals_id = db.add_vitals(CHAT, weight_kg=76.0)
    future = (dt.date.today() + dt.timedelta(days=5)).isoformat()
    row = db.edit_vitals_date(CHAT, vitals_id, future)
    assert row["vitals_date"] == db.today_str()


def test_delete_and_restore_vitals_roundtrip():
    vitals_id = db.add_vitals(CHAT, weight_kg=76.6, sleep_hours=5.5, knee_pain=2)
    deleted = db.delete_vitals(CHAT, vitals_id)
    assert db.get_vitals(CHAT, vitals_id) is None
    restored = db.restore_deleted_vitals(CHAT, deleted)
    assert restored["weight_kg"] == 76.6
    assert restored["knee_pain"] == 2
    assert restored["id"] != vitals_id  # fresh id, same as delete_meal's documented behavior


def test_delete_most_recent_vitals():
    db.add_vitals(CHAT, weight_kg=76.6)
    db.add_vitals(CHAT, weight_kg=76.4)
    deleted = db.delete_most_recent_vitals(CHAT)
    assert deleted["weight_kg"] == 76.4
    assert db.get_recent_vitals(CHAT)[0]["weight_kg"] == 76.6


# ---------- ai.py: extract_vitals / parse_message ----------

def test_extract_vitals_happy_path(monkeypatch):
    import json
    payload = {"weight_kg": 76.6, "sleep_hours": 5.5, "knee_pain": 2, "notes": None}
    _mock_client(monkeypatch, json.dumps(payload))
    result = ai.extract_vitals("weight 76.6, slept 5.5 hours, knee 2/10")
    assert result["weight_kg"] == 76.6
    assert result["knee_pain"] == 2


def test_extract_vitals_never_raises_on_api_failure(monkeypatch):
    _mock_client(monkeypatch, ConnectionError("network blip"))
    result = ai.extract_vitals("weight 76.6")
    assert result["weight_kg"] is None
    assert result["notes"] == "weight 76.6"  # raw text preserved rather than lost


def test_parse_message_passes_recent_vitals_into_the_prompt(monkeypatch):
    import json
    from test_ai import _FakeClient
    captured = {}

    class _CapturingClient(_FakeClient):
        def create(self, **kwargs):
            captured["messages"] = kwargs.get("messages")
            return super().create(**kwargs)

    fake = _CapturingClient(json.dumps({"intent": "casual", "casual_reply": "hey!"}))
    monkeypatch.setattr(ai, "_get_client", lambda: fake)
    recent_vitals = [{"id": 4, "weight_kg": 76.6, "sleep_hours": 5.5,
                       "knee_pain": 2, "vitals_date": "2026-09-02"}]
    ai.parse_message("hi", [], [], [], recent_vitals)
    sent = captured["messages"][0]["content"]
    assert "76.6" in sent


# ---------- bot.py: natural-language, command, and correction/undo ----------

def test_natural_language_log_vitals(monkeypatch):
    db.get_or_create_user(CHAT)

    def fake_parse_message(text, recent_expenses=None, recent_meals=None,
                            recent_workouts=None, recent_vitals=None):
        return {"intent": "log_vitals", "weight_kg": 76.6, "sleep_hours": 5.5,
                "knee_pain": 2, "vitals_notes": None,
                "clarification_question": None, "casual_reply": None}

    monkeypatch.setattr(bot.ai, "parse_message", fake_parse_message)
    update = FakeUpdate(CHAT, text="weight 76.6, slept 5.5 hours, knee 2/10")
    _run(bot.handle_text(update, FakeContext()))
    assert any("76.6" in r for r in update.message.replies)
    assert db.get_recent_vitals(CHAT)[0]["weight_kg"] == 76.6


def test_logvitals_command_uses_extract_vitals(monkeypatch):
    db.get_or_create_user(CHAT)
    monkeypatch.setattr(bot.ai, "extract_vitals", lambda desc: {
        "weight_kg": 76.6, "sleep_hours": 5.5, "knee_pain": 2, "notes": None,
    })
    update = FakeUpdate(CHAT)
    context = FakeContext()
    context.args = ["weight", "76.6,", "knee", "2/10"]
    _run(bot.logvitals_cmd(update, context))
    assert any("76.6" in r for r in update.message.replies)
    assert db.get_recent_vitals(CHAT)[0]["knee_pain"] == 2


def test_correction_can_target_vitals_by_domain(monkeypatch):
    """Cross-domain version of the meal/workout correction tests: a vitals
    check-in can be deleted by natural language too, dispatched via
    target_domain, exercising the shared _DOMAIN_OPS registry path."""
    db.get_or_create_user(CHAT)
    vitals_id = db.add_vitals(CHAT, weight_kg=76.6, sleep_hours=5.5, knee_pain=2)

    def fake_parse_message(text, recent_expenses=None, recent_meals=None,
                            recent_workouts=None, recent_vitals=None):
        return {
            "intent": "correction", "target_domain": "vitals", "target_expense_id": vitals_id,
            "correction_action": "delete", "days_ago": None,
            "clarification_question": None, "casual_reply": None,
        }

    monkeypatch.setattr(bot.ai, "parse_message", fake_parse_message)
    update = FakeUpdate(CHAT, text="delete that check-in, logged it twice")
    _run(bot.handle_text(update, FakeContext()))
    assert db.get_vitals(CHAT, vitals_id) is None
    assert any("Deleted" in r for r in update.message.replies)


def test_undo_reverts_a_vitals_deletion(monkeypatch):
    db.get_or_create_user(CHAT)
    vitals_id = db.add_vitals(CHAT, weight_kg=76.6, sleep_hours=5.5, knee_pain=2)

    def fake_parse_message(text, recent_expenses=None, recent_meals=None,
                            recent_workouts=None, recent_vitals=None):
        return {
            "intent": "correction", "target_domain": "vitals", "target_expense_id": vitals_id,
            "correction_action": "delete", "days_ago": None,
            "clarification_question": None, "casual_reply": None,
        }

    monkeypatch.setattr(bot.ai, "parse_message", fake_parse_message)
    context = FakeContext()
    delete_update = FakeUpdate(CHAT, text="delete that check-in")
    _run(bot.handle_text(delete_update, context))
    assert db.get_vitals(CHAT, vitals_id) is None

    undo_update = FakeUpdate(CHAT, text="undo")
    _run(bot.handle_text(undo_update, context))
    assert db.get_recent_vitals(CHAT)[0]["weight_kg"] == 76.6


def test_undo_reverts_a_vitals_date_edit(monkeypatch):
    """Exercises the edit_date branch of _revert_last_correction's
    _DOMAIN_OPS-registry path (delete is covered above; edit_date wasn't
    exercised yet for any of the three simple domains)."""
    db.get_or_create_user(CHAT)
    vitals_id = db.add_vitals(CHAT, weight_kg=76.6)
    original_date = db.get_vitals(CHAT, vitals_id)["vitals_date"]

    def fake_parse_message(text, recent_expenses=None, recent_meals=None,
                            recent_workouts=None, recent_vitals=None):
        return {
            "intent": "correction", "target_domain": "vitals", "target_expense_id": vitals_id,
            "correction_action": "edit_date", "days_ago": 1,
            "clarification_question": None, "casual_reply": None,
        }

    monkeypatch.setattr(bot.ai, "parse_message", fake_parse_message)
    context = FakeContext()
    edit_update = FakeUpdate(CHAT, text="that check-in was actually yesterday")
    _run(bot.handle_text(edit_update, context))
    assert db.get_vitals(CHAT, vitals_id)["vitals_date"] != original_date

    undo_update = FakeUpdate(CHAT, text="undo")
    _run(bot.handle_text(undo_update, context))
    assert db.get_vitals(CHAT, vitals_id)["vitals_date"] == original_date
