"""
Tests for the evening "quiet day" nudge (nudges.py): fires once a day only
when literally nothing (no meal, workout, or vitals check-in) has been
logged today, and never for a partial day. Same per-chat try/except
discipline as morning.morning_briefing_tick/app.rollover_tick -- one chat's
failure must never block the nudge reaching everyone else. The nudge text
itself is a fixed deterministic string -- no AI call decides WHETHER or
WHAT to send -- but it IS run through Morrow's companion voice before
sending (see replies._send_proactive), same as every other reply; tests
rely on tests/conftest.py's autouse passthrough mock for that by default,
same as the rest of the suite.
"""

import asyncio

import ai
import bot
import db
from conftest import CHAT
from test_morning import _FakeTickContext

CHAT_B = CHAT + 1


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_evening_nudge_fires_for_a_genuinely_quiet_day():
    db.get_or_create_user(CHAT)
    context = _FakeTickContext()
    _run(bot.evening_nudge_tick(context))
    sent_chat_ids = {chat_id for chat_id, _ in context.bot.sent}
    assert CHAT in sent_chat_ids


def test_evening_nudge_does_not_fire_once_anything_at_all_is_logged():
    """Deliberately conservative -- a workout logged but no meals yet is a
    completely normal partial day, not something to nag about."""
    db.get_or_create_user(CHAT)
    db.add_workout(CHAT, "tennis", 60)
    context = _FakeTickContext()
    _run(bot.evening_nudge_tick(context))
    sent_chat_ids = {chat_id for chat_id, _ in context.bot.sent}
    assert CHAT not in sent_chat_ids


def test_evening_nudge_does_not_fire_when_only_a_meal_is_logged():
    db.get_or_create_user(CHAT)
    db.add_meal(CHAT, "Lunch", ["chicken rice"], 500, 700, 600)
    context = _FakeTickContext()
    _run(bot.evening_nudge_tick(context))
    sent_chat_ids = {chat_id for chat_id, _ in context.bot.sent}
    assert CHAT not in sent_chat_ids


def test_evening_nudge_does_not_fire_when_only_vitals_are_logged():
    db.get_or_create_user(CHAT)
    db.add_vitals(CHAT, weight_kg=76.6)
    context = _FakeTickContext()
    _run(bot.evening_nudge_tick(context))
    sent_chat_ids = {chat_id for chat_id, _ in context.bot.sent}
    assert CHAT not in sent_chat_ids


def test_evening_nudge_is_narrated(monkeypatch):
    """Regression guard: the nudge must go through replies._send_proactive
    (not a bare context.bot.send_message) so it gets the companion voice
    too. Uses a non-identity fake narrate_reply (unlike the autouse
    passthrough fixture) so a regression here can actually be detected."""
    db.get_or_create_user(CHAT)
    monkeypatch.setattr(ai, "narrate_reply", lambda text, *a, **kw: f"(companion voice) {text}")
    context = _FakeTickContext()
    _run(bot.evening_nudge_tick(context))
    assert context.bot.sent[0][1].startswith("(companion voice) Quiet day")


def test_evening_nudge_logs_to_conversation_history():
    db.get_or_create_user(CHAT)
    context = _FakeTickContext()
    _run(bot.evening_nudge_tick(context))
    rows = db.get_recent_messages(CHAT)
    assert rows[-1]["role"] == "morrow"
    assert "Quiet day" in rows[-1]["content"]


def test_evening_nudge_one_chats_failure_does_not_block_the_rest():
    db.get_or_create_user(CHAT)
    db.get_or_create_user(CHAT_B)
    context = _FakeTickContext(fail_for_chat_id=CHAT)
    _run(bot.evening_nudge_tick(context))
    sent_chat_ids = {chat_id for chat_id, _ in context.bot.sent}
    assert sent_chat_ids == {CHAT_B}
