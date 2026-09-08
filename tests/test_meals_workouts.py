"""
Tests for the meals/workouts additions: db.py CRUD, ai.py's extraction
resilience (text, vision, and workout), and bot.py's natural-language and
photo logging paths, including cross-domain corrections.

Same discipline as the rest of the suite: no real network, no real Claude
calls (ai._get_client is always mocked), throwaway SQLite per test.
"""

import asyncio
import datetime as dt

import pytest

import ai
import bot
import db
from conftest import CHAT
from test_ai import _FakeClient, _mock_client  # reuse the existing fake client


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------- db.py: meals ----------

def test_add_and_get_meal_roundtrip():
    meal_id = db.add_meal(CHAT, "Snack", ["coke zero", "750ml water"], 0, 0, 0, water_ml=750)
    row = db.get_meal(CHAT, meal_id)
    assert row["items"] == ["coke zero", "750ml water"]
    assert row["water_ml"] == 750
    assert row["meal_date"] == db.today_str()


def test_daily_meal_totals_sum_same_day_only():
    db.add_meal(CHAT, "Snack", ["coke zero"], 0, 0, 0, water_ml=750)
    db.add_meal(CHAT, "Dinner", ["rice", "chicken"], 990, 1360, 1170, water_ml=None)
    yesterday = dt.date.today() - dt.timedelta(days=1)
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO meals (chat_id, meal_type, items, calories_estimate, meal_date) "
            "VALUES (?, 'Snack', '[]', 500, ?)",
            (CHAT, yesterday.isoformat()),
        )
    totals = db.get_daily_meal_totals(CHAT, db.today_str())
    assert totals["calories"] == 1170  # today's two entries only, not yesterday's 500
    assert totals["water_ml"] == 750


def test_edit_meal_date_clamps_future_to_today():
    meal_id = db.add_meal(CHAT, "Snack", ["mango"], 90, 120, 105)
    future = (dt.date.today() + dt.timedelta(days=5)).isoformat()
    row = db.edit_meal_date(CHAT, meal_id, future)
    assert row["meal_date"] == db.today_str()


def test_delete_and_restore_meal_roundtrip():
    meal_id = db.add_meal(CHAT, "Dinner", ["rice"], 200, 300, 250, water_ml=None)
    deleted = db.delete_meal(CHAT, meal_id)
    assert db.get_meal(CHAT, meal_id) is None
    restored = db.restore_deleted_meal(CHAT, deleted)
    assert restored["items"] == ["rice"]
    assert restored["calories_estimate"] == 250
    assert restored["id"] != meal_id  # fresh id, same as delete_expense's documented behavior


def test_delete_most_recent_meal():
    db.add_meal(CHAT, "Snack", ["first"], 10, 10, 10)
    db.add_meal(CHAT, "Snack", ["second"], 20, 20, 20)
    deleted = db.delete_most_recent_meal(CHAT)
    assert deleted["items"] == ["second"]
    assert [m["items"][0] for m in db.get_recent_meals(CHAT)] == ["first"]


# ---------- db.py: workouts ----------

def test_add_and_get_workout_roundtrip():
    workout_id = db.add_workout(CHAT, "tennis", duration_min=60, distance_km=None, notes="won 2 sets")
    row = db.get_workout(CHAT, workout_id)
    assert row["activity"] == "tennis"
    assert row["duration_min"] == 60
    assert row["notes"] == "won 2 sets"
    assert row["workout_date"] == db.today_str()


def test_edit_workout_date_and_delete_restore():
    workout_id = db.add_workout(CHAT, "run", distance_km=2.4)
    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    moved = db.edit_workout_date(CHAT, workout_id, yesterday)
    assert moved["workout_date"] == yesterday

    deleted = db.delete_workout(CHAT, workout_id)
    assert db.get_workout(CHAT, workout_id) is None
    restored = db.restore_deleted_workout(CHAT, deleted)
    assert restored["activity"] == "run"
    assert restored["distance_km"] == 2.4


# ---------- ai.py: extract_meal / extract_meal_from_image / extract_workout ----------

def test_extract_meal_happy_path(monkeypatch):
    import json
    payload = {"meal_type": "Snack", "items": ["mango"], "calories_low": 90,
               "calories_high": 120, "calories_estimate": 105, "water_ml": None}
    _mock_client(monkeypatch, json.dumps(payload))
    result = ai.extract_meal("had a mango")
    assert result["items"] == ["mango"]
    assert result["calories_estimate"] == 105


def test_extract_meal_never_raises_on_api_failure(monkeypatch):
    _mock_client(monkeypatch, ConnectionError("network blip"))
    result = ai.extract_meal("some meal")
    assert result["calories_estimate"] is None
    assert result["items"] == ["some meal"]  # raw text preserved rather than lost


def test_extract_meal_from_image_sends_image_content_block(monkeypatch):
    """Regression guard mirroring test_parse_message_passes_recent_expenses_into_the_prompt:
    the image must actually be sent as an image content block, not just the caption text."""
    import json
    captured = {}

    class _CapturingClient(_FakeClient):
        def create(self, **kwargs):
            captured["messages"] = kwargs.get("messages")
            return super().create(**kwargs)

    payload = {"meal_type": "Dinner", "items": ["rice", "chicken"], "calories_low": 990,
               "calories_high": 1360, "calories_estimate": 1170, "water_ml": None}
    fake = _CapturingClient(json.dumps(payload))
    monkeypatch.setattr(ai, "_get_client", lambda: fake)

    result = ai.extract_meal_from_image(b"fake-jpeg-bytes", caption="my usual dinner")
    content_blocks = captured["messages"][0]["content"]
    types = [b["type"] for b in content_blocks]
    assert "image" in types
    assert content_blocks[types.index("image")]["source"]["media_type"] == "image/jpeg"
    assert result["calories_estimate"] == 1170


def test_extract_meal_from_image_never_raises_on_api_failure(monkeypatch):
    _mock_client(monkeypatch, TypeError("boom"))
    result = ai.extract_meal_from_image(b"bytes", caption="dinner")
    assert result["calories_estimate"] is None
    assert result["items"] == ["dinner"]


def test_extract_workout_happy_path(monkeypatch):
    import json
    payload = {"activity": "tennis", "duration_min": 60, "distance_km": None, "notes": "won 2 sets"}
    _mock_client(monkeypatch, json.dumps(payload))
    result = ai.extract_workout("played tennis for an hour, won 2 sets")
    assert result["activity"] == "tennis"
    assert result["duration_min"] == 60


def test_extract_workout_never_raises_on_api_failure(monkeypatch):
    _mock_client(monkeypatch, ConnectionError("network blip"))
    result = ai.extract_workout("gym leg day")
    assert result["activity"] == "gym leg day"
    assert result["notes"] == "gym leg day"


def test_parse_message_passes_recent_meals_and_workouts_into_the_prompt(monkeypatch):
    import json
    captured = {}

    class _CapturingClient(_FakeClient):
        def create(self, **kwargs):
            captured["messages"] = kwargs.get("messages")
            return super().create(**kwargs)

    fake = _CapturingClient(json.dumps({
        "intent": "casual", "casual_reply": "hey!",
    }))
    monkeypatch.setattr(ai, "_get_client", lambda: fake)
    recent_meals = [{"id": 7, "items": ["mango"], "meal_type": "Snack",
                      "calories_estimate": 105, "meal_date": "2026-09-03"}]
    recent_workouts = [{"id": 3, "activity": "tennis", "duration_min": 60, "workout_date": "2026-09-02"}]
    ai.parse_message("hi", [], recent_meals, recent_workouts)
    sent = captured["messages"][0]["content"]
    assert "mango" in sent
    assert "tennis" in sent


# ---------- bot.py: natural-language and photo logging ----------

def _log_meal_response(**overrides):
    base = {
        "intent": "log_meal", "meal_type": "Snack", "items": ["mango"], "meal_items": ["mango"],
        "calories_low": 90, "calories_high": 120, "calories_estimate": 105, "water_ml": None,
        "clarification_question": None, "casual_reply": None,
    }
    base.update(overrides)
    return base


class FakeMessage:
    def __init__(self, text=None, caption=None, photo=None):
        self.text = text
        self.caption = caption
        self.photo = photo or []
        self.replies = []

    async def reply_text(self, text):
        self.replies.append(text)


class FakeChat:
    def __init__(self, chat_id):
        self.id = chat_id


class FakeUpdate:
    def __init__(self, chat_id, text=None, caption=None, photo=None):
        self.message = FakeMessage(text=text, caption=caption, photo=photo)
        self.effective_chat = FakeChat(chat_id)


class FakeContext:
    def __init__(self):
        self.chat_data = {}
        self.args = []


def test_natural_language_log_meal_updates_running_total(monkeypatch):
    db.get_or_create_user(CHAT)

    def fake_parse_message(text, recent_expenses=None, recent_meals=None, recent_workouts=None, recent_vitals=None,
                            recent_messages=None, memory_list=None):
        return _log_meal_response()

    monkeypatch.setattr(bot.ai, "parse_message", fake_parse_message)
    update = FakeUpdate(CHAT, text="had a mango")
    _run(bot.handle_text(update, FakeContext()))
    assert any("mango" in r for r in update.message.replies)
    assert any("running total" in r.lower() for r in update.message.replies)
    assert db.get_recent_meals(CHAT)[0]["items"] == ["mango"]


def test_logmeal_command_uses_extract_meal(monkeypatch):
    db.get_or_create_user(CHAT)
    monkeypatch.setattr(bot.ai, "extract_meal", lambda desc: {
        "meal_type": "Dinner", "items": ["rice", "chicken"],
        "calories_low": 400, "calories_high": 600, "calories_estimate": 500, "water_ml": None,
    })
    update = FakeUpdate(CHAT)
    context = FakeContext()
    context.args = ["rice", "and", "chicken"]
    _run(bot.logmeal_cmd(update, context))
    assert any("rice, chicken" in r for r in update.message.replies)
    assert db.get_recent_meals(CHAT)[0]["calories_estimate"] == 500


class _FakeTelegramFile:
    async def download_as_bytearray(self):
        return bytearray(b"fake-jpeg-bytes")


class _FakePhotoSize:
    async def get_file(self):
        return _FakeTelegramFile()


def test_photo_message_logs_a_meal(monkeypatch):
    db.get_or_create_user(CHAT)
    captured = {}

    def fake_extract_from_image(image_bytes, caption=None):
        captured["image_bytes"] = image_bytes
        captured["caption"] = caption
        return {"meal_type": "Dinner", "items": ["rice", "sambal sotong", "fried chicken"],
                "calories_low": 990, "calories_high": 1360, "calories_estimate": 1170, "water_ml": None}

    monkeypatch.setattr(bot.ai, "extract_meal_from_image", fake_extract_from_image)
    update = FakeUpdate(CHAT, caption="dinner at home", photo=[_FakePhotoSize()])
    _run(bot.handle_photo(update, FakeContext()))
    assert captured["caption"] == "dinner at home"
    assert captured["image_bytes"] == b"fake-jpeg-bytes"
    assert any("1170" in r or "990" in r for r in update.message.replies)
    assert db.get_recent_meals(CHAT)[0]["calories_estimate"] == 1170


def test_natural_language_log_workout(monkeypatch):
    db.get_or_create_user(CHAT)

    def fake_parse_message(text, recent_expenses=None, recent_meals=None, recent_workouts=None, recent_vitals=None,
                            recent_messages=None, memory_list=None):
        return {"intent": "log_workout", "activity": "tennis", "duration_min": 60,
                "distance_km": None, "workout_notes": "won 2 sets",
                "clarification_question": None, "casual_reply": None}

    monkeypatch.setattr(bot.ai, "parse_message", fake_parse_message)
    update = FakeUpdate(CHAT, text="played tennis for an hour, won 2 sets")
    _run(bot.handle_text(update, FakeContext()))
    assert any("tennis" in r for r in update.message.replies)
    assert db.get_recent_workouts(CHAT)[0]["activity"] == "tennis"


def test_correction_can_target_a_meal_by_domain(monkeypatch):
    """Cross-domain version of the existing expense date-correction test:
    a meal can be deleted by natural language too, dispatched via target_domain."""
    db.get_or_create_user(CHAT)
    meal_id = db.add_meal(CHAT, "Snack", ["duplicate mango"], 90, 120, 105)

    def fake_parse_message(text, recent_expenses=None, recent_meals=None, recent_workouts=None, recent_vitals=None,
                            recent_messages=None, memory_list=None):
        return {
            "intent": "correction", "target_domain": "meal", "target_expense_id": meal_id,
            "correction_action": "delete", "days_ago": None,
            "clarification_question": None, "casual_reply": None,
        }

    monkeypatch.setattr(bot.ai, "parse_message", fake_parse_message)
    update = FakeUpdate(CHAT, text="delete that mango, I logged it twice")
    _run(bot.handle_text(update, FakeContext()))
    assert db.get_meal(CHAT, meal_id) is None
    assert any("Deleted" in r for r in update.message.replies)


def test_undo_reverts_a_meal_deletion(monkeypatch):
    db.get_or_create_user(CHAT)
    meal_id = db.add_meal(CHAT, "Snack", ["mango"], 90, 120, 105)

    def fake_parse_message(text, recent_expenses=None, recent_meals=None, recent_workouts=None, recent_vitals=None,
                            recent_messages=None, memory_list=None):
        return {
            "intent": "correction", "target_domain": "meal", "target_expense_id": meal_id,
            "correction_action": "delete", "days_ago": None,
            "clarification_question": None, "casual_reply": None,
        }

    monkeypatch.setattr(bot.ai, "parse_message", fake_parse_message)
    context = FakeContext()
    delete_update = FakeUpdate(CHAT, text="delete that mango")
    _run(bot.handle_text(delete_update, context))
    assert db.get_meal(CHAT, meal_id) is None

    undo_update = FakeUpdate(CHAT, text="undo")
    _run(bot.handle_text(undo_update, context))
    assert db.get_recent_meals(CHAT)[0]["items"] == ["mango"]
