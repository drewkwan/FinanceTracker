"""
Tests for the evening "quiet day" nudge (nudges.py): fires once a day only
when literally nothing (no meal, workout, or vitals check-in) has been
logged today, and never for a partial day. Same per-chat try/except
discipline as morning.morning_briefing_tick/app.rollover_tick -- one chat's
failure must never block the nudge reaching everyone else. No AI call
involved at all (same reasoning as morning.py), so nothing here mocks
ai._get_client.
"""

import asyncio

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


def test_evening_nudge_one_chats_failure_does_not_block_the_rest():
    db.get_or_create_user(CHAT)
    db.get_or_create_user(CHAT_B)
    context = _FakeTickContext(fail_for_chat_id=CHAT)
    _run(bot.evening_nudge_tick(context))
    sent_chat_ids = {chat_id for chat_id, _ in context.bot.sent}
    assert sent_chat_ids == {CHAT_B}
